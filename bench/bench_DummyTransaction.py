# -*- coding:UTF-8 -*-
from constants import ITERATIONS
snowman = u'☃'

def run():
    from Cheetah.DummyTransaction import DummyTransaction
    trans = DummyTransaction()
    write = trans.response().write

    write(u'\nsnowman 1: ' + snowman)
    _ = [
        write(u'\nsnowman ' + str(i) + ': ' + snowman)
        for i in range(2, ITERATIONS)
    ]

    ret = trans.response().getvalue()
    assert snowman in ret
    assert len(ret) > ITERATIONS
    return ret
