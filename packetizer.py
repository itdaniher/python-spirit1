import spi
s = spi.SPI('/dev/spidev32765.0', 0, 100000)
from bitarray import bitarray

from statistics import mode, mean, StatisticsError

import json
import time
import itertools

printer = lambda xs: ''.join([{0: '░', 1: '█', 2: '╳'}[x] for x in xs])
debinary = lambda ba: sum([x*(2**i) for (i,x) in enumerate(reversed(ba))])

import tsd_client

ilen = lambda it: sum(1 for _ in it)
rle = lambda xs: ((ilen(gp), x) for x, gp in itertools.groupby(xs))
rld = lambda xs: itertools.chain.from_iterable(itertools.repeat(x, n) for n, x in xs)

class PacketBase(object):
    def __init__(self, packet = [], errors = None, deciles = {}, raw = []):
        self.packet = packet
        self.errors = errors
        self.deciles = deciles
        self.raw = raw

def get_decile_durations(pulses):
    values = set([value for (width, value) in pulses])
    deciles = {}
    if len(pulses) < 10:
        return None
    for value in sorted(list(values)):
        counts = sorted([width for (width, x) in pulses if x == value])
        tenth = len(counts) // 10
        if not tenth:
            return None
        short_decile = int(mean(counts[1*tenth:2*tenth]))
        long_decile = int(mean(counts[8*tenth:9*tenth]))
        deciles[value] = (short_decile, long_decile)
    return deciles

def find_pulse_groups(pulses, deciles):
    # find segments of quiet that are 9x longer than the short period
    # this is naive, if a trivial pulse width encoding is used, any sequence of 9 or more short sequential silences will be read as a packet break
    breaks = [i[0] for i in enumerate(pulses) if (i[1][0] > min(deciles[0][0],deciles[1][0])  * 9) and (i[1][1] == False)]
    # find periodicity of the packets
    break_deltas = [y-x for (x,y) in zip(breaks, breaks[1::])]
    if len(break_deltas) < 2:
        return None
    # ignoring few-pulse packets, if you have more than three different fragment sizes, try to regularize
    elif len(set([bd for bd in break_deltas if bd > 3])) > 3:
        try:
            d_mode = mode(break_deltas)
        # if all values different, use mean as mode
        except StatisticsError:
            d_mode = round(mean(break_deltas))
        # determine expected periodicity of packet widths
        breaks2 = [x*d_mode for x in range(round(max(breaks) // d_mode))]
        if len(breaks2) < 2:
            return None
        # discard breaks more than 10% from expected position
        breaks = [x for x in breaks if True in [abs(x-y) < breaks2[1]//10 for y in breaks2]]
        # define packet pulses as the segment between breaks
    return breaks

def demodulator(pulses):
    packets = []
    # drop short (clearly erroneous, spurious) pulses
    pulses = [x for x in rle(rld([x for x in pulses if x[0] > 2]))]
    deciles = get_decile_durations(pulses)
    if not deciles:
        return packets
    breaks = find_pulse_groups(pulses, deciles)
    if not breaks:
        return packets
    for (x,y) in zip(breaks, breaks[1::]):
        packet = pulses[x+1:y]
        pb = []
        errors = []
        # iterate over packet pulses
        for chip in packet:
            valid = False
            for v in deciles.keys():
                for (i, width) in enumerate(deciles[v]):
                    if (not valid) and (chip[1] == v) and (abs(chip[0] - width) < width // 2):
                        pb += [v]*(i+1)
                        valid = True
            if not valid:
                errors += [chip]
                pb += [2]
        if len(pb) > 4:
            result = PacketBase(pb, errors, deciles, pulses[x:y])
            packets.append(result)
    return packets

ba = bitarray(endian='big')

def silver_sensor(packet):
    if packet.errors == []:
        bits = [x[0] == 2 for x in rle(packet.packet) if x[1] == 0]
        # some thanks to http://forum.iobroker.net/viewtopic.php?t=3818
        # "TTTT=Binär in Dez., Dez als HEX, HEX in Dez umwandeln (zB 0010=2Dez, 2Dez=2 Hex) 0010=2 1001=9 0110=6 => 692 HEX = 1682 Dez = >1+6= 7 UND 82 = 782°F"
        if len(bits) == 42:
            fields = [0,2,8,2,2,4,4,4,4,4,8]
            fields = [x for x in itertools.accumulate(fields)]
            results = [debinary(bits[x:y]) for (x,y) in zip(fields, fields[1:])]
            # uid is never 0xff, but similar protocols sometimes decode with this field as 0xFF
            if results[1] == 255:
                return None
            temp = (16**2*results[6]+16*results[5]+results[4])
            humidity = (16*results[8]+results[7])
            if temp > 1000:
                temp %= 1000
                temp += 100
            temp /= 10
            temp -= 32
            temp *= 5/9
            return {'uid':results[1], 'temperature': temp, 'humidity': humidity, 'channel':results[3], 'metameta': packet.__dict__}
    return None

# block size
bs = 32768

last = {}
while True:
    p = s.transfer([0]*bs)
    # if input values are all-high or all-low
    ba = bitarray(endian='big')
    ba.frombytes(bytes(p))
    pulses = [(w,v*1) for (w,v) in rle(ba)]
    if len(pulses) > 10:
        current_time = time.time()
        for packet in demodulator(pulses):
            print(printer(packet.packet))
            send_and_update = False
            res = silver_sensor(packet)
            if res is not None:
                uid = res['uid']
            else:
                uid = None
            if (uid in last.keys()) and ((time.time() - last[uid]) > 30):
                send_and_update = True
            else:
                last[uid] = time.time()
            if (res is not None) and send_and_update:
                last[uid] = time.time()
                tsd_client.log(res)
