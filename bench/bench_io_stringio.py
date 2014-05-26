# -*- coding:UTF-8 -*-
from constants import ITERATIONS
snowman = u'☃'

from io import StringIO

def run():
    f = StringIO() #u'snowman 1: ' + snowman)
    f.write(u'\nsnowman 1: ' + snowman)
    [
            f.write(u'\nsnowman ' + str(i) + ': ' + snowman)
            for i in range(2, ITERATIONS)
    ]
    
    ret = f.getvalue()
    assert snowman in ret
    assert len(ret) > ITERATIONS
    return ret
