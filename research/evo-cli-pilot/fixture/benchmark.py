import argparse
from target import parse
from inline_instrumentation import log_task, write_result

TRAIN = [('1,2', [1,2]), ('', []), ('  ', []), ('0', [0]), ('-1,2', [-1,2]),
         (' 3 , 4 ', [3,4]), ('5,5', [5,5]), ('+7', [7]), ('9,-9,0', [9,-9,0]),
         ('1000000', [1000000]), ('1\n,2', [1,2]), ('-0', [0])]
HELD_OUT = [('8,13', [8,13]), ('-42', [-42]), ('\t', []), ('+11, -12', [11,-12]),
            ('1,,2', ValueError), ('nope', ValueError)]
p = argparse.ArgumentParser()
p.add_argument('--gate', action='store_true')
a = p.parse_args()
for i, (text, expected) in enumerate(HELD_OUT if a.gate else TRAIN):
    try:
        actual = parse(text)
    except ValueError:
        actual = ValueError
    passed = actual == expected
    log_task(str(i), float(passed), summary='synthetic parser correctness')
score = write_result()
if a.gate and score < 1:
    raise SystemExit(1)
