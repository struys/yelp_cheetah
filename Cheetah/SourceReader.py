"""SourceReader class for Cheetah's Parser and CodeGenerator
"""
import re

EOLre = re.compile(r'[ \f\t]*(?:\r\n|\r|\n)')
EOLZre = re.compile(r'(?:\r\n|\r|\n|\Z)')


class SourceReader(object):
    def __init__(self, src, filename=None, encoding=None):
        self._src = src
        self._filename = filename
        self._breakPoint = len(self._src)
        self._pos = 0

        # collect some meta-information
        self._EOLs = []
        pos = 0
        while pos < len(self):
            EOLmatch = EOLZre.search(src, pos)
            self._EOLs.append(EOLmatch.start())
            pos = EOLmatch.end()

        self._BOLs = []
        for pos in self._EOLs:
            BOLpos = self.findBOL(pos)
            self._BOLs.append(BOLpos)

    def src(self):
        return self._src

    def filename(self):
        return self._filename

    def __len__(self):
        return self._breakPoint

    def __getslice__(self, i, j):
        i = max(i, 0)
        j = max(j, 0)
        return self._src[i:j]

    def splitlines(self):
        if not hasattr(self, '_srcLines'):
            self._srcLines = self._src.splitlines()
        return self._srcLines

    def lineNum(self, pos):
        for i in range(len(self._BOLs)):
            if pos >= self._BOLs[i] and pos <= self._EOLs[i]:
                return i

    def getRowCol(self, pos=None):
        if pos is None:
            pos = self._pos
        lineNum = self.lineNum(pos)
        BOL = self._BOLs[lineNum]
        return lineNum + 1, pos - BOL + 1

    def getRowColLine(self):
        row, col = self.getRowCol(self._pos)
        return row, col, self.splitlines()[row - 1]

    def pos(self):
        return self._pos

    def setPos(self, pos):
        self.checkPos(pos)
        self._pos = pos

    def validPos(self, pos):
        return pos <= self._breakPoint and pos >= 0

    def checkPos(self, pos):
        if not pos <= self._breakPoint:
            raise AssertionError(
                "pos ({0}) is invalid: beyond the stream's end ({1})".format(
                    pos, self._breakPoint - 1
                )
            )
        elif not pos >= 0:
            raise AssertionError("pos (" + str(pos) + ") is invalid: less than 0")

    def breakPoint(self):
        return self._breakPoint

    def setBreakPoint(self, pos):
        if pos > len(self._src):
            raise AssertionError(
                "New breakpoint ({0}) is invalid: beyond the end of stream's "
                "source string ({1})".format(pos, len(self._src))
            )
        elif not pos >= 0:
            raise AssertionError(
                "New breakpoint (" + str(pos) + ") is invalid: less than 0"
            )

        self._breakPoint = pos

    def atEnd(self):
        return self._pos >= self._breakPoint

    def peek(self, offset=0):
        self.checkPos(self._pos + offset)
        pos = self._pos + offset
        return self._src[pos]

    def getc(self):
        pos = self._pos
        if self.validPos(pos + 1):
            self._pos += 1
        return self._src[pos]

    def advance(self, offset=1):
        self.checkPos(self._pos + offset)
        self._pos += offset

    def rev(self, offset=1):
        self.checkPos(self._pos - offset)
        self._pos -= offset

    def readTo(self, to, start=None):
        self.checkPos(to)
        if start is None:
            start = self._pos
        self._pos = to
        return self._src[start:to]

    def readToEOL(self, start=None, gobble=True):
        EOLmatch = EOLZre.search(self.src(), self.pos())
        if gobble:
            pos = EOLmatch.end()
        else:
            pos = EOLmatch.start()
        return self.readTo(to=pos, start=start)

    def find(self, it, pos=None):
        if pos is None:
            pos = self._pos
        return self._src.find(it, pos)

    def startswith(self, it, pos=None):
        if self.find(it, pos) == self.pos():
            return True
        else:
            return False

    def findBOL(self, pos=None):
        if pos is None:
            pos = self._pos
        src = self.src()
        return max(src.rfind('\n', 0, pos)+1, src.rfind('\r', 0, pos) + 1, 0)

    def findEOL(self, gobble=False):
        match = EOLZre.search(self.src(), self._pos)
        if gobble:
            return match.end()
        else:
            return match.start()

    def isLineClearToPos(self, pos=None):
        if pos is None:
            pos = self.pos()
        self.checkPos(pos)
        src = self.src()
        BOL = self.findBOL()
        return BOL == pos or src[BOL:pos].isspace()

    def matchWhiteSpace(self, WSchars=' \f\t'):
        return (not self.atEnd()) and self.peek() in WSchars

    def getWhiteSpace(self, max=None, WSchars=' \f\t'):
        if not self.matchWhiteSpace(WSchars):
            return ''
        start = self.pos()
        breakPoint = self.breakPoint()
        if max is not None:
            breakPoint = min(breakPoint, self.pos() + max)
        while self.pos() < breakPoint:
            self.advance()
            if not self.matchWhiteSpace(WSchars):
                break
        return self.src()[start:self.pos()]