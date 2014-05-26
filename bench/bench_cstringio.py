# -*- coding:UTF-8 -*-
from constants import ITERATIONS
snowman = u'☃'

from cStringIO import StringIO as cStringIO

class StringIO(object):
    def __init__(self):
        self.o = cStringIO()
    def write(self, s):
        self.o.write(s.encode('UTF-8'))
    def getvalue(self):
        return self.o.getvalue().decode('UTF-8')



def run():
    f = StringIO() #u'snowman 1: ' + snowman)
    f.write(u'\nsnowman 1: ' + snowman)
    _ = [
        f.write(u'\nsnowman ' + str(i) + ': ' + snowman)
        for i in range(2, ITERATIONS)
    ]

    ret = f.getvalue()
    assert snowman in ret
    assert len(ret) > ITERATIONS
    return ret
