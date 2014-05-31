"""
Parser classes for Cheetah's Compiler

Classes:
  ParseError(Exception)
  _LowLevelParser(Cheetah.SourceReader.SourceReader), basically a lexer
  Parser(_LowLevelParser)
"""

import sys
import re
import types
from tokenize import pseudoprog
import inspect

from Cheetah.SourceReader import SourceReader
from Cheetah.Unspecified import Unspecified


SET_LOCAL = 0
SET_GLOBAL = 1
SET_MODULE = 2

##################################################
# Tokens for the parser

# generic
identchars = "abcdefghijklmnopqrstuvwxyz" \
             "ABCDEFGHIJKLMNOPQRSTUVWXYZ_"
namechars = identchars + "0123456789"

# operators
powerOp = '**'
unaryArithOps = ('+', '-', '~')
binaryArithOps = ('+', '-', '/', '//', '%')
shiftOps = ('>>', '<<')
bitwiseOps = ('&', '|', '^')
assignOp = '='
augAssignOps = ('+=', '-=', '/=', '*=', '**=', '^=', '%=',
                '>>=', '<<=', '&=', '|=', )
assignmentOps = (assignOp,) + augAssignOps

compOps = ('<', '>', '==', '!=', '<=', '>=', '<>', 'is', 'in',)
booleanOps = ('and', 'or', 'not')
operators = (
    (powerOp,) + unaryArithOps + binaryArithOps + shiftOps + bitwiseOps +
    assignmentOps + compOps + booleanOps
)

delimeters = ('(', ')', '{', '}', '[', ']',
              ',', '.', ':', ';', '=', '`') + augAssignOps


keywords = ('and',       'del',       'for',       'is',        'raise',
            'assert',    'elif',      'from',      'lambda',    'return',
            'break',     'else',      'global',    'not',       'try',
            'class',     'except',    'if',        'or',        'while',
            'continue',  'exec',      'import',    'pass',
            'def',       'finally',   'in',        'print',
            )

single3 = "'''"
double3 = '"""'

tripleQuotedStringStarts = ("'''", '"""',
                            "r'''", 'r"""', "R'''", 'R"""',
                            "u'''", 'u"""', "U'''", 'U"""',
                            "ur'''", 'ur"""', "Ur'''", 'Ur"""',
                            "uR'''", 'uR"""', "UR'''", 'UR"""')

tripleQuotedStringPairs = {"'''": single3, '"""': double3,
                           "r'''": single3, 'r"""': double3,
                           "u'''": single3, 'u"""': double3,
                           "ur'''": single3, 'ur"""': double3,
                           "R'''": single3, 'R"""': double3,
                           "U'''": single3, 'U"""': double3,
                           "uR'''": single3, 'uR"""': double3,
                           "Ur'''": single3, 'Ur"""': double3,
                           "UR'''": single3, 'UR"""': double3,
                           }

closurePairs = {')': '(', ']': '[', '}': '{'}
closurePairsRev = {'(': ')', '[': ']', '{': '}'}

##################################################
# Regex chunks for the parser

tripleQuotedStringREs = {}


def makeTripleQuoteRe(start, end):
    start = re.escape(start)
    end = re.escape(end)
    return re.compile(r'(?:' + start + r').*?' + r'(?:' + end + r')', re.DOTALL)

for start, end in tripleQuotedStringPairs.items():
    tripleQuotedStringREs[start] = makeTripleQuoteRe(start, end)

WS = r'[ \f\t]*'
EOL = r'\r\n|\n|\r'
escCharLookBehind = r'(?:(?<=\A)|(?<!\\))'
nameCharLookAhead = r'(?=[A-Za-z_])'
identRE = re.compile(r'[a-zA-Z_][a-zA-Z_0-9]*')
directiveRE = re.compile(r'([a-zA-Z_][a-zA-Z0-9_-]*|@[a-zA-Z_][a-zA-Z0-9_]*)')
EOLre = re.compile(r'(?:\r\n|\r|\n)')

escapedNewlineRE = re.compile(r'(?<!\\)((\\\\)*)\\(n|012)')

# TODO(buck): audit all directives. delete with prejudice.
directiveNamesAndParsers = {
    # importing and inheritance
    'import': None,
    'from': None,
    'extends': 'eatExtends',
    'implements': 'eatImplements',
    'super': 'eatSuper',

    # output, filtering, and caching
    'slurp': 'eatSlurp',
    'filter': 'eatFilter',
    'silent': None,

    'call': 'eatCall',

    # declaration, assignment, and deletion
    'attr': 'eatAttr',
    'def': 'eatDef',
    'block': 'eatBlock',
    '@': 'eatDecorator',

    'set': 'eatSet',
    'del': None,

    # flow control
    'if': 'eatIf',
    'while': None,
    'for': None,
    'else': None,
    'elif': None,
    'pass': None,
    'break': None,
    'continue': None,
    'return': None,
    'yield': None,

    # error handling
    'assert': None,
    'raise': None,
    'try': None,
    'except': None,
    'finally': None,

    # intructions to the parser and compiler
    'compiler-settings': 'eatCompilerSettings',

    # misc
    'encoding': 'eatEncoding',

    'end': 'eatEndDirective',
    }

endDirectiveNamesAndHandlers = {
    'def': 'handleEndDef',      # has short-form
    'block': None,              # has short-form
    'call': None,               # has short-form
    'filter': None,
    'while': None,              # has short-form
    'for': None,                # has short-form
    'if': None,                 # has short-form
    'try': None,                # has short-form
    }

##################################################
# CLASSES


# @@TR: SyntaxError doesn't call exception.__str__ for some reason!
# class ParseError(SyntaxError):
class ParseError(ValueError):
    def __init__(self, stream, msg='Invalid Syntax', extMsg='', lineno=None, col=None):
        self.stream = stream
        if stream.pos() >= len(stream):
            stream.setPos(len(stream) - 1)
        self.msg = msg
        self.extMsg = extMsg
        self.lineno = lineno
        self.col = col

    def __str__(self):
        return self.report()

    def report(self):
        stream = self.stream
        if stream.filename():
            f = " in file %s" % stream.filename()
        else:
            f = ''
        report = ''
        if self.lineno:
            lineno = self.lineno
            row, col, line = (lineno, (self.col or 0),
                              self.stream.splitlines()[lineno-1])
        else:
            row, col, line = self.stream.getRowColLine()

        # get the surrounding lines
        lines = stream.splitlines()
        prevLines = []                  # (rowNum, content)
        for i in range(1, 4):
            if row - 1 - i <= 0:
                break
            prevLines.append((row - i, lines[row - 1 - i]))

        nextLines = []                  # (rowNum, content)
        for i in range(1, 4):
            if not row - 1 + i < len(lines):
                break
            nextLines.append((row + i, lines[row - 1 + i]))
        nextLines.reverse()

        # print the main message
        report += "\n\n%s\n" % self.msg
        report += "Line %i, column %i%s\n\n" % (row, col, f)
        report += 'Line|Cheetah Code\n'
        report += '----|-------------------------------------------------------------\n'
        while prevLines:
            lineInfo = prevLines.pop()
            report += "%(row)-4d|%(line)s\n" % {'row': lineInfo[0], 'line': lineInfo[1]}
        report += "%(row)-4d|%(line)s\n" % {'row': row, 'line': line}
        report += ' ' * 5 + ' ' * (col - 1) + "^\n"

        while nextLines:
            lineInfo = nextLines.pop()
            report += "%(row)-4d|%(line)s\n" % {'row': lineInfo[0], 'line': lineInfo[1]}
        # add the extra msg
        if self.extMsg:
            report += self.extMsg + '\n'

        return report


class UnknownDirectiveError(ParseError):
    pass


class ArgList(object):
    """Used by _LowLevelParser.getArgList()"""

    def __init__(self):
        self.arguments = []
        self.defaults = []
        self.count = 0

    def add_argument(self, name):
        self.arguments.append(name)
        self.defaults.append(None)

    def next(self):
        self.count += 1

    def add_default(self, token):
        count = self.count
        if self.defaults[count] is None:
            self.defaults[count] = ''
        self.defaults[count] += token

    def merge(self):
        defaults = (isinstance(d, basestring) and d.strip() or None for d in self.defaults)
        return list(map(None, (a.strip() for a in self.arguments), defaults))


class _LowLevelParser(SourceReader):
    """This class implements the methods to match or extract ('get*') the basic
    elements of Cheetah's grammar.  It does NOT handle any code generation or
    state management.
    """

    _settingsManager = None

    def setSettingsManager(self, settingsManager):
        self._settingsManager = settingsManager

    def setting(self, key):
        return self._settingsManager.setting(key)

    def setSetting(self, key, val):
        self._settingsManager.setSetting(key, val)

    def settings(self):
        return self._settingsManager.settings()

    def updateSettings(self, settings):
        self._settingsManager.updateSettings(settings)

    def _initializeSettings(self):
        self._settingsManager._initializeSettings()

    def configureParser(self):
        """Is called by the Compiler instance after the parser has had a
        settingsManager assigned with self.setSettingsManager()
        """
        self._makeCheetahVarREs()
        self._makeCommentREs()
        self._makeDirectiveREs()
        self._makePspREs()
        self._possibleNonStrConstantChars = (
            self.setting('commentStartToken')[0] +
            self.setting('cheetahVarStartToken')[0] +
            self.setting('directiveStartToken')[0] +
            self.setting('PSPStartToken')[0])
        self._nonStrConstMatchers = [
            self.matchCommentStartToken,
            self.matchVariablePlaceholderStart,
            self.matchExpressionPlaceholderStart,
            self.matchDirective,
            self.matchPSPStartToken,
        ]

    # regex setup

    def _makeCheetahVarREs(self):
        """Setup the regexs for Cheetah $var parsing."""

        self.cheetahVarStartRE = re.compile(
            escCharLookBehind +
            r'(?P<startToken>' + re.escape(self.setting('cheetahVarStartToken')) + ')' +
            r'(?P<enclosure>|(?:(?:\{|\(|\[)[ \t\f]*))' +  # allow WS after enclosure
            r'(?=[A-Za-z_])')
        validCharsLookAhead = r'(?=[A-Za-z_\*!\{\(\[])'
        self.cheetahVarStartToken = self.setting('cheetahVarStartToken')
        self.cheetahVarStartTokenRE = re.compile(
            escCharLookBehind +
            re.escape(self.setting('cheetahVarStartToken'))
            + validCharsLookAhead
            )

        self.cheetahVarInExpressionStartTokenRE = re.compile(
            re.escape(self.setting('cheetahVarStartToken'))
            + r'(?=[A-Za-z_])'
            )

        self.expressionPlaceholderStartRE = re.compile(
            escCharLookBehind +
            r'(?P<startToken>' + re.escape(self.setting('cheetahVarStartToken')) + ')' +
            # r'\[[ \t\f]*'
            r'(?:\{|\(|\[)[ \t\f]*'
            + r'(?=[^\)\}\]])'
            )

    def _makeCommentREs(self):
        """Construct the regex bits that are used in comment parsing."""
        startTokenEsc = re.escape(self.setting('commentStartToken'))
        self.commentStartTokenRE = re.compile(escCharLookBehind + startTokenEsc)

    def _makeDirectiveREs(self):
        """Construct the regexs that are used in directive parsing."""
        startToken = self.setting('directiveStartToken')
        endToken = self.setting('directiveEndToken')
        startTokenEsc = re.escape(startToken)
        endTokenEsc = re.escape(endToken)
        validSecondCharsLookAhead = r'(?=[A-Za-z_@])'
        reParts = [escCharLookBehind, startTokenEsc]
        reParts.append(validSecondCharsLookAhead)
        self.directiveStartTokenRE = re.compile(''.join(reParts))
        self.directiveEndTokenRE = re.compile(escCharLookBehind + endTokenEsc)

    def _makePspREs(self):
        """Setup the regexs for PSP parsing."""
        startToken = self.setting('PSPStartToken')
        startTokenEsc = re.escape(startToken)
        self.PSPStartTokenRE = re.compile(escCharLookBehind + startTokenEsc)
        endToken = self.setting('PSPEndToken')
        endTokenEsc = re.escape(endToken)
        self.PSPEndTokenRE = re.compile(escCharLookBehind + endTokenEsc)

    def _unescapeCheetahVars(self, theString):
        """Unescape any escaped Cheetah \$vars in the string.
        """

        token = self.setting('cheetahVarStartToken')
        return theString.replace('\\' + token, token)

    def _unescapeDirectives(self, theString):
        """Unescape any escaped Cheetah directives in the string.
        """

        token = self.setting('directiveStartToken')
        return theString.replace('\\' + token, token)

    def isLineClearToStartToken(self, pos=None):
        return self.isLineClearToPos(pos)

    def matchTopLevelToken(self):
        """Returns the first match found from the following methods:
            self.matchCommentStartToken
            self.matchVariablePlaceholderStart
            self.matchExpressionPlaceholderStart
            self.matchDirective
            self.matchPSPStartToken

        Returns None if no match.
        """
        match = None
        if self.peek() in self._possibleNonStrConstantChars:
            for matcher in self._nonStrConstMatchers:
                match = matcher()
                if match:
                    break
        return match

    def matchPyToken(self):
        match = pseudoprog.match(self.src(), self.pos())

        if match and match.group() in tripleQuotedStringStarts:
            TQSmatch = tripleQuotedStringREs[match.group()].match(self.src(), self.pos())
            if TQSmatch:
                return TQSmatch
        return match

    def getPyToken(self):
        match = self.matchPyToken()
        if match is None:
            raise ParseError(self)
        elif match.group() in tripleQuotedStringStarts:
            raise ParseError(self, msg='Malformed triple-quoted string')
        return self.readTo(match.end())

    def matchCommentStartToken(self):
        return self.commentStartTokenRE.match(self.src(), self.pos())

    def getCommentStartToken(self):
        match = self.matchCommentStartToken()
        assert match
        return self.readTo(match.end())

    def getDottedName(self):
        srcLen = len(self)
        nameChunks = []

        if not self.peek() in identchars:
            raise ParseError(self)

        while self.pos() < srcLen:
            c = self.peek()
            if c in namechars:
                nameChunk = self.getIdentifier()
                nameChunks.append(nameChunk)
            elif c == '.':
                if self.pos() + 1 < srcLen and self.peek(1) in identchars:
                    nameChunks.append(self.getc())
                else:
                    break
            else:
                break

        return ''.join(nameChunks)

    def matchIdentifier(self):
        return identRE.match(self.src(), self.pos())

    def getIdentifier(self):
        match = self.matchIdentifier()
        if not match:
            raise ParseError(self, 'Invalid identifier')
        return self.readTo(match.end())

    def matchAssignmentOperator(self):
        match = self.matchPyToken()
        if match and match.group() not in assignmentOps:
            match = None
        return match

    def getAssignmentOperator(self):
        match = self.matchAssignmentOperator()
        assert match
        return self.readTo(match.end())

    def matchDirective(self):
        """Returns False or the name of the directive matched.
        """
        startPos = self.pos()
        if not self.matchDirectiveStartToken():
            return False
        self.getDirectiveStartToken()
        directiveName = self.matchDirectiveName()
        self.setPos(startPos)
        return directiveName

    def matchDirectiveName(self):
        directive_match = directiveRE.match(self.src(), self.pos())
        # There is a case where something looks like a decorator but actually
        # isn't a decorator.  The parsing for this is particularly wonky
        if directive_match is None:
            return None

        match_text = directive_match.group(0)

        # #@ is the "directive" for decorators
        if match_text.startswith('@'):
            return '@'
        elif match_text in self._directiveNamesAndParsers:
            return match_text
        else:
            raise UnknownDirectiveError(
                self,
                'Bad macro name: "{0}". '
                'You may want to escape that # sign?'.format(match_text),
            )

    def matchDirectiveStartToken(self):
        return self.directiveStartTokenRE.match(self.src(), self.pos())

    def getDirectiveStartToken(self):
        match = self.matchDirectiveStartToken()
        assert match
        return self.readTo(match.end())

    def matchDirectiveEndToken(self):
        return self.directiveEndTokenRE.match(self.src(), self.pos())

    def getDirectiveEndToken(self):
        match = self.matchDirectiveEndToken()
        assert match
        return self.readTo(match.end())

    def matchColonForSingleLineShortFormDirective(self):
        if not self.atEnd() and self.peek() == ':':
            restOfLine = self[self.pos() + 1:self.findEOL()]
            restOfLine = restOfLine.strip()
            if not restOfLine:
                return False
            elif self.commentStartTokenRE.match(restOfLine):
                return False
            else:  # non-whitespace, non-commment chars found
                return True
        return False

    def matchPSPStartToken(self):
        return self.PSPStartTokenRE.match(self.src(), self.pos())

    def matchPSPEndToken(self):
        return self.PSPEndTokenRE.match(self.src(), self.pos())

    def getPSPStartToken(self):
        match = self.matchPSPStartToken()
        assert match
        return self.readTo(match.end())

    def getPSPEndToken(self):
        match = self.matchPSPEndToken()
        assert match
        return self.readTo(match.end())

    def matchCheetahVarStart(self):
        """includes the enclosure"""
        return self.cheetahVarStartRE.match(self.src(), self.pos())

    def matchCheetahVarStartToken(self):
        """includes the enclosure"""
        return self.cheetahVarStartTokenRE.match(self.src(), self.pos())

    def matchCheetahVarInExpressionStartToken(self):
        """no enclosures"""
        return self.cheetahVarInExpressionStartTokenRE.match(self.src(), self.pos())

    def matchVariablePlaceholderStart(self):
        """includes the enclosure"""
        return self.cheetahVarStartRE.match(self.src(), self.pos())

    def matchExpressionPlaceholderStart(self):
        """includes the enclosure"""
        return self.expressionPlaceholderStartRE.match(self.src(), self.pos())

    def getCheetahVarStartToken(self):
        """just the start token, not the enclosure"""
        match = self.matchCheetahVarStartToken()
        assert match
        return self.readTo(match.end())

    def getTargetVarsList(self):
        varnames = []
        while not self.atEnd():
            if self.peek() in ' \t\f':
                self.getWhiteSpace()
            elif self.peek() in '\r\n':
                break
            elif self.startswith(','):
                self.advance()
            elif self.startswith('in ') or self.startswith('in\t'):
                break
            elif self.matchCheetahVarInExpressionStartToken():
                self.getCheetahVarStartToken()
                varnames.append(self.getDottedName())
            elif self.matchIdentifier():
                varnames.append(self.getDottedName())
            else:
                break
        return varnames

    def getCheetahVar(self, plain=False, skipStartToken=False):
        """This is called when parsing inside expressions.
        """
        if not skipStartToken:
            self.getCheetahVarStartToken()
        return self.getCheetahVarBody(plain=plain)

    def getCheetahVarBody(self, plain=False):
        # @@TR: this should be in the compiler
        return self._compiler.genCheetahVar(self.getCheetahVarNameChunks(), plain=plain)

    def getCheetahVarNameChunks(self):

        """
        nameChunks = list of Cheetah $var subcomponents represented as tuples
          [ (namemapperPart,autoCall,restOfName),
          ]
        where:
          namemapperPart = the dottedName base
          autocall = where NameMapper should use autocalling on namemapperPart
          restOfName = any arglist, index, or slice

        If restOfName contains a call arglist (e.g. '(1234)') then autocall is
        False, otherwise it defaults to True.

        EXAMPLE
        ------------------------------------------------------------------------

        if the raw CheetahVar is
          $a.b.c[1].d().x.y.z

        nameChunks is the list
          [ ('a.b.c',True,'[1]'),
            ('d',False,'()'),
            ('x.y.z',True,''),
          ]

        """
        chunks = []
        while self.pos() < len(self):
            rest = ''
            autoCall = True
            if not self.peek() in identchars + '.':
                break
            elif self.peek() == '.':

                if self.pos() + 1 < len(self) and self.peek(1) in identchars:
                    self.advance()  # discard the period as it isn't needed with NameMapper
                else:
                    break

            dottedName = self.getDottedName()
            if not self.atEnd() and self.peek() in '([':
                if self.peek() == '(':
                    rest = self.getCallArgString()
                else:
                    rest = self.getExpression(enclosed=True)

                period = max(dottedName.rfind('.'), 0)
                if period:
                    chunks.append((dottedName[:period], autoCall, ''))
                    dottedName = dottedName[period + 1:]
                if rest and rest[0] == '(':
                    autoCall = False
            chunks.append((dottedName, autoCall, rest))

        return chunks

    def getCallArgString(self,
                         enclosures=[],  # list of tuples (char, pos), where char is ({ or [
                         useNameMapper=Unspecified):

        """ Get a method/function call argument string.

        This method understands *arg, and **kw
        """

        # @@TR: this settings mangling should be removed
        if useNameMapper is not Unspecified:
            useNameMapper_orig = self.setting('useNameMapper')
            self.setSetting('useNameMapper', useNameMapper)

        if enclosures:
            pass
        else:
            if not self.peek() == '(':
                raise ParseError(self, msg="Expected '('")
            startPos = self.pos()
            self.getc()
            enclosures = [('(', startPos),
                          ]

        argStringBits = ['(']
        addBit = argStringBits.append

        while True:
            if self.atEnd():
                open = enclosures[-1][0]
                close = closurePairsRev[open]
                self.setPos(enclosures[-1][1])
                raise ParseError(
                    self, msg="EOF was reached before a matching '" + close +
                    "' was found for the '" + open + "'")

            c = self.peek()
            if c in ")}]":  # get the ending enclosure and break
                if not enclosures:
                    raise ParseError(self)
                c = self.getc()
                open = closurePairs[c]
                if enclosures[-1][0] == open:
                    enclosures.pop()
                    addBit(')')
                    break
                else:
                    raise ParseError(self)
            elif c in " \t\f\r\n":
                addBit(self.getc())
            elif self.matchCheetahVarInExpressionStartToken():
                startPos = self.pos()
                codeFor1stToken = self.getCheetahVar()
                WS = self.getWhiteSpace()
                if not self.atEnd() and self.peek() == '=':
                    nextToken = self.getPyToken()
                    if nextToken == '=':
                        endPos = self.pos()
                        self.setPos(startPos)
                        codeFor1stToken = self.getCheetahVar(plain=True)
                        self.setPos(endPos)

                    # finally
                    addBit(codeFor1stToken + WS + nextToken)
                else:
                    addBit(codeFor1stToken + WS)
            elif self.matchCheetahVarStart():
                # it has syntax that is only valid at the top level
                self._raiseErrorAboutInvalidCheetahVarSyntaxInExpr()
            else:
                token = self.getPyToken()
                if token in ('{', '(', '['):
                    self.rev()
                    token = self.getExpression(enclosed=True)
                addBit(token)

        if useNameMapper is not Unspecified:
            self.setSetting('useNameMapper', useNameMapper_orig)  # @@TR: see comment above

        return ''.join(argStringBits)

    def getDefArgList(self, exitPos=None, useNameMapper=False):

        """ Get an argument list. Can be used for method/function definition
        argument lists or for # directive argument lists. Returns a list of
        tuples in the form (argName, defVal=None) with one tuple for each arg
        name.

        These defVals are always strings, so (argName, defVal=None) is safe even
        with a case like (arg1, arg2=None, arg3=1234*2), which would be returned as
        [('arg1', None),
         ('arg2', 'None'),
         ('arg3', '1234*2'),
        ]

        This method understands *arg, and **kw
        """
        if self.peek() == '(':
            self.advance()
        else:
            exitPos = self.findEOL()  # it's a directive so break at the EOL
        argList = ArgList()
        onDefVal = False

        # @@TR: this settings mangling should be removed
        useNameMapper_orig = self.setting('useNameMapper')
        self.setSetting('useNameMapper', useNameMapper)

        while True:
            if self.atEnd():
                raise ParseError(
                    self, msg="EOF was reached before a matching ')'" +
                    " was found for the '('")

            if self.pos() == exitPos:
                break

            c = self.peek()
            if c == ")" or self.matchDirectiveEndToken():
                break
            elif c == ":":
                break
            elif c in " \t\f\r\n":
                if onDefVal:
                    argList.add_default(c)
                self.advance()
            elif c == '=':
                onDefVal = True
                self.advance()
            elif c == ",":
                argList.next()
                onDefVal = False
                self.advance()
            elif self.startswith(self.cheetahVarStartToken) and not onDefVal:
                self.advance(len(self.cheetahVarStartToken))
            elif self.matchIdentifier() and not onDefVal:
                argList.add_argument(self.getIdentifier())
            elif onDefVal:
                if self.matchCheetahVarInExpressionStartToken():
                    token = self.getCheetahVar()
                elif self.matchCheetahVarStart():
                    # it has syntax that is only valid at the top level
                    self._raiseErrorAboutInvalidCheetahVarSyntaxInExpr()
                else:
                    token = self.getPyToken()
                    if token in ('{', '(', '['):
                        self.rev()
                        token = self.getExpression(enclosed=True)
                argList.add_default(token)
            elif c == '*' and not onDefVal:
                varName = self.getc()
                if self.peek() == '*':
                    varName += self.getc()
                if not self.matchIdentifier():
                    raise ParseError(self)
                varName += self.getIdentifier()
                argList.add_argument(varName)
            else:
                raise ParseError(self)

        self.setSetting('useNameMapper', useNameMapper_orig)  # @@TR: see comment above
        return argList.merge()

    def getExpressionParts(self,
                           enclosed=False,
                           enclosures=None,  # list of tuples (char, pos), where char is ({ or [
                           pyTokensToBreakAt=None,  # only works if not enclosed
                           useNameMapper=Unspecified,
                           ):

        """ Get a Cheetah expression that includes $CheetahVars and break at
        directive end tokens, the end of an enclosure, or at a specified
        pyToken.
        """

        if useNameMapper is not Unspecified:
            useNameMapper_orig = self.setting('useNameMapper')
            self.setSetting('useNameMapper', useNameMapper)

        if enclosures is None:
            enclosures = []

        srcLen = len(self)
        exprBits = []
        while True:
            if self.atEnd():
                if enclosures:
                    open = enclosures[-1][0]
                    close = closurePairsRev[open]
                    self.setPos(enclosures[-1][1])
                    raise ParseError(
                        self, msg="EOF was reached before a matching '" + close +
                        "' was found for the '" + open + "'")
                else:
                    break

            c = self.peek()
            if c in "{([":
                exprBits.append(c)
                enclosures.append((c, self.pos()))
                self.advance()
            elif enclosed and not enclosures:
                break
            elif c in "])}":
                if not enclosures:
                    raise ParseError(self)
                open = closurePairs[c]
                if enclosures[-1][0] == open:
                    enclosures.pop()
                    exprBits.append(c)
                else:
                    open = enclosures[-1][0]
                    close = closurePairsRev[open]
                    row, col = self.getRowCol()
                    self.setPos(enclosures[-1][1])
                    raise ParseError(
                        self, msg="A '" + c + "' was found at line " + str(row) +
                        ", col " + str(col) +
                        " before a matching '" + close +
                        "' was found\nfor the '" + open + "'")
                self.advance()

            elif c in " \f\t":
                exprBits.append(self.getWhiteSpace())
            elif self.matchDirectiveEndToken() and not enclosures:
                break
            elif c == "\\" and self.pos() + 1 < srcLen:
                eolMatch = EOLre.match(self.src(), self.pos() + 1)
                if not eolMatch:
                    self.advance()
                    raise ParseError(self, msg='Line ending expected')
                self.setPos(eolMatch.end())
            elif c in '\r\n':
                if enclosures:
                    self.advance()
                else:
                    break
            elif self.matchCheetahVarInExpressionStartToken():
                expr = self.getCheetahVar()
                exprBits.append(expr)
            elif self.matchCheetahVarStart():
                # it has syntax that is only valid at the top level
                self._raiseErrorAboutInvalidCheetahVarSyntaxInExpr()
            else:
                beforeTokenPos = self.pos()
                token = self.getPyToken()
                if (
                    not enclosures and
                    pyTokensToBreakAt and
                    token in pyTokensToBreakAt
                ):
                    self.setPos(beforeTokenPos)
                    break

                exprBits.append(token)
                if identRE.match(token):
                    if token == 'for':
                        expr = self.getExpression(useNameMapper=False, pyTokensToBreakAt=['in'])
                        exprBits.append(expr)
                    else:
                        exprBits.append(self.getWhiteSpace())
                        if not self.atEnd() and self.peek() == '(':
                            exprBits.append(self.getCallArgString())

        if useNameMapper is not Unspecified:
            self.setSetting('useNameMapper', useNameMapper_orig)  # @@TR: see comment above
        return exprBits

    def getExpression(self,
                      enclosed=False,
                      enclosures=None,  # list of tuples (char, pos), where # char is ({ or [
                      pyTokensToBreakAt=None,
                      useNameMapper=Unspecified,
                      ):
        """Returns the output of self.getExpressionParts() as a concatenated
        string rather than as a list.
        """
        return ''.join(self.getExpressionParts(
            enclosed=enclosed, enclosures=enclosures,
            pyTokensToBreakAt=pyTokensToBreakAt,
            useNameMapper=useNameMapper))

    def _raiseErrorAboutInvalidCheetahVarSyntaxInExpr(self):
        match = self.matchCheetahVarStart()
        groupdict = match.groupdict()
        if groupdict.get('enclosure'):
            raise ParseError(
                self,
                msg='Long-form placeholders - ${}, $(), $[], etc. are not valid inside expressions. '
                'Use them in top-level $placeholders only.')
        else:
            raise ParseError(
                self,
                msg='This form of $placeholder syntax is not valid here.')

    def getPlaceholder(self, plain=False):
        startPos = self.pos()
        lineCol = self.getRowCol(startPos)
        self.getCheetahVarStartToken()

        if self.peek() in '({[':
            pos = self.pos()
            enclosureOpenChar = self.getc()
            enclosures = [(enclosureOpenChar, pos)]
            self.getWhiteSpace()
        else:
            enclosures = []

        filterArgs = None
        if self.matchIdentifier():
            nameChunks = self.getCheetahVarNameChunks()
            expr = self._compiler.genCheetahVar(nameChunks[:], plain=plain)
            restOfExpr = None
            if enclosures:
                WS = self.getWhiteSpace()
                expr += WS
                if self.peek() == closurePairsRev[enclosureOpenChar]:
                    self.getc()
                else:
                    restOfExpr = self.getExpression(enclosed=True, enclosures=enclosures)
                    if restOfExpr[-1] == closurePairsRev[enclosureOpenChar]:
                        restOfExpr = restOfExpr[:-1]
                    expr += restOfExpr
            rawPlaceholder = self[startPos: self.pos()]
        else:
            expr = self.getExpression(enclosed=True, enclosures=enclosures)
            if expr[-1] == closurePairsRev[enclosureOpenChar]:
                expr = expr[:-1]
            rawPlaceholder = self[startPos: self.pos()]

        return (expr, rawPlaceholder, lineCol, filterArgs)


class Parser(_LowLevelParser):
    """This class is a StateMachine for parsing Cheetah source and
    sending state dependent code generation commands to
    Cheetah.Compiler.Compiler.
    """
    def __init__(self, src, filename=None, compiler=None):
        super(Parser, self).__init__(src, filename=filename)
        self.setSettingsManager(compiler)
        self._compiler = compiler
        self.setupState()
        self.configureParser()

    def setupState(self):
        self._macros = {}
        self._macroDetails = {}
        self._openDirectivesStack = []

    def cleanup(self):
        """Cleanup to remove any possible reference cycles
        """
        self._macros.clear()
        for macroname, macroDetails in self._macroDetails.items():
            del macroDetails.template
        self._macroDetails.clear()

    def configureParser(self):
        super(Parser, self).configureParser()
        self._initDirectives()

    def _initDirectives(self):
        def normalizeParserVal(val):
            if isinstance(val, (str, unicode)):
                handler = getattr(self, val)
            elif isinstance(val, type):
                handler = val(self)
            elif hasattr(val, '__call__'):
                handler = val
            elif val is None:
                handler = val
            else:
                raise Exception('Invalid parser/handler value %r for %s' % (val, name))
            return handler

        normalizeHandlerVal = normalizeParserVal

        _directiveNamesAndParsers = directiveNamesAndParsers.copy()

        _endDirectiveNamesAndHandlers = endDirectiveNamesAndHandlers.copy()

        self._directiveNamesAndParsers = {}
        for name, val in _directiveNamesAndParsers.items():
            if val in (False, 0):
                continue
            self._directiveNamesAndParsers[name] = normalizeParserVal(val)

        self._endDirectiveNamesAndHandlers = {}
        for name, val in _endDirectiveNamesAndHandlers.items():
            if val in (False, 0):
                continue
            self._endDirectiveNamesAndHandlers[name] = normalizeHandlerVal(val)

        self._closeableDirectives = ['def', 'block',
                                     'call',
                                     'filter',
                                     'if',
                                     'for', 'while',
                                     'try',
                                     ]

        for macroName, callback in self.setting('macroDirectives').items():
            if isinstance(callback, type):
                callback = callback(parser=self)
            assert callback
            self._macros[macroName] = callback
            self._directiveNamesAndParsers[macroName] = self.eatMacroCall

    # main parse loop

    def parse(self, breakPoint=None, assertEmptyStack=True):
        if breakPoint:
            origBP = self.breakPoint()
            self.setBreakPoint(breakPoint)
            assertEmptyStack = False

        while not self.atEnd():
            if self.matchCommentStartToken():
                self.eatComment()
            elif self.matchVariablePlaceholderStart():
                self.eatPlaceholder()
            elif self.matchExpressionPlaceholderStart():
                self.eatPlaceholder()
            elif self.matchDirective():
                self.eatDirective()
            elif self.matchPSPStartToken():
                self.eatPSP()
            else:
                self.eatPlainText()
        if assertEmptyStack:
            self.assertEmptyOpenDirectivesStack()
        if breakPoint:
            self.setBreakPoint(origBP)

    # non-directive eat methods

    def eatPlainText(self):
        startPos = self.pos()
        match = None
        while not self.atEnd():
            match = self.matchTopLevelToken()
            if match:
                break
            else:
                self.advance()
        strConst = self.readTo(self.pos(), start=startPos)
        strConst = self._unescapeCheetahVars(strConst)
        strConst = self._unescapeDirectives(strConst)
        self._compiler.addStrConst(strConst)
        return match

    def eatComment(self):
        isLineClearToStartToken = self.isLineClearToStartToken()
        if isLineClearToStartToken:
            self._compiler.handleWSBeforeDirective()
        self.getCommentStartToken()
        comm = self.readToEOL(gobble=isLineClearToStartToken)
        self._compiler.addComment(comm)

    def eatPlaceholder(self):
        (expr, rawPlaceholder, lineCol, filterArgs) = self.getPlaceholder()

        self._compiler.addPlaceholder(
            expr,
            filterArgs=filterArgs,
            rawPlaceholder=rawPlaceholder,
            lineCol=lineCol,
        )
        return

    def eatPSP(self):
        self.getPSPStartToken()
        endToken = self.setting('PSPEndToken')
        startPos = self.pos()
        while not self.atEnd():
            if self.peek() == endToken[0]:
                if self.matchPSPEndToken():
                    break
            self.advance()
        pspString = self.readTo(self.pos(), start=startPos).strip()
        self._compiler.addPSP(pspString)
        self.getPSPEndToken()

    # generic directive eat methods
    _simpleIndentingDirectives = '''
    else elif for while try except finally'''.split()
    _simpleExprDirectives = '''
    pass continue return yield break
    del assert raise
    silent
    import from'''.split()
    _directiveHandlerNames = {'import': 'addImportStatement',
                              'from': 'addImportStatement', }

    def eatDirective(self):
        directiveName = self.matchDirective()

        # subclasses can override the default behaviours here by providing an
        # eater method in self._directiveNamesAndParsers[directiveName]
        directiveParser = self._directiveNamesAndParsers.get(directiveName)
        if directiveParser:
            directiveParser()
        elif directiveName in self._simpleIndentingDirectives:
            handlerName = self._directiveHandlerNames.get(directiveName)
            if not handlerName:
                handlerName = 'add' + directiveName.capitalize()
            handler = getattr(self._compiler, handlerName)
            self.eatSimpleIndentingDirective(directiveName, callback=handler)
        elif directiveName in self._simpleExprDirectives:
            handlerName = self._directiveHandlerNames.get(directiveName)
            if not handlerName:
                handlerName = 'add' + directiveName.capitalize()
            handler = getattr(self._compiler, handlerName)
            if directiveName == 'silent':
                includeDirectiveNameInExpr = False
            else:
                includeDirectiveNameInExpr = True
            expr = self.eatSimpleExprDirective(
                directiveName,
                includeDirectiveNameInExpr=includeDirectiveNameInExpr)
            handler(expr)

    def _eatRestOfDirectiveTag(self, isLineClearToStartToken, endOfFirstLinePos):
        foundComment = False
        # There's a potential ambiguity when parsing comments on directived
        # lines.
        # The difficult thing to differentiate is between the following
        # cases:
        # 1. #if foo:##end if#
        # Here, the part that begins with ## is matched as a comment but is
        # actually a directive
        # 2. #if foo: ##comment
        # Here it is actually a comment, but (potentially) ParseErrors as a
        # missing directive.
        if self.matchCommentStartToken():
            pos = self.pos()
            self.advance()

            try:
                matched_directive = self.matchDirective()
            except UnknownDirectiveError:
                matched_directive = False

            if not matched_directive:
                self.setPos(pos)
                foundComment = True
                self.eatComment()  # this won't gobble the EOL
            else:
                self.setPos(pos)

        if not foundComment and self.matchDirectiveEndToken():
            self.getDirectiveEndToken()
        elif isLineClearToStartToken and (not self.atEnd()) and self.peek() in '\r\n':
            # still gobble the EOL if a comment was found.
            self.readToEOL(gobble=True)

        if isLineClearToStartToken and (self.atEnd() or self.pos() > endOfFirstLinePos):
            self._compiler.handleWSBeforeDirective()

    def _eatToThisEndDirective(self, directiveName):
        finalPos = endRawPos = startPos = self.pos()
        directiveChar = self.setting('directiveStartToken')[0]
        isLineClearToStartToken = False
        while not self.atEnd():
            if self.peek() == directiveChar:
                if self.matchDirective() == 'end':
                    endRawPos = self.pos()
                    self.getDirectiveStartToken()
                    self.advance(len('end'))
                    self.getWhiteSpace()
                    if self.startswith(directiveName):
                        if self.isLineClearToStartToken(endRawPos):
                            isLineClearToStartToken = True
                            endRawPos = self.findBOL(endRawPos)
                        self.advance(len(directiveName))  # to end of directiveName
                        self.getWhiteSpace()
                        finalPos = self.pos()
                        break
            self.advance()
            finalPos = endRawPos = self.pos()

        textEaten = self.readTo(endRawPos, start=startPos)
        self.setPos(finalPos)

        endOfFirstLinePos = self.findEOL()

        if self.matchDirectiveEndToken():
            self.getDirectiveEndToken()
        elif isLineClearToStartToken and (not self.atEnd()) and self.peek() in '\r\n':
            self.readToEOL(gobble=True)

        if isLineClearToStartToken and self.pos() > endOfFirstLinePos:
            self._compiler.handleWSBeforeDirective()
        return textEaten

    def eatSimpleExprDirective(self, directiveName, includeDirectiveNameInExpr=True):
        isLineClearToStartToken = self.isLineClearToStartToken()
        endOfFirstLine = self.findEOL()
        self.getDirectiveStartToken()
        if not includeDirectiveNameInExpr:
            self.advance(len(directiveName))
        expr = self.getExpression().strip()
        directiveName = expr.split()[0]
        if directiveName in self._closeableDirectives:
            self.pushToOpenDirectivesStack(directiveName)
        self._eatRestOfDirectiveTag(isLineClearToStartToken, endOfFirstLine)
        return expr

    def eatSimpleIndentingDirective(self, directiveName, callback,
                                    includeDirectiveNameInExpr=False):
        isLineClearToStartToken = self.isLineClearToStartToken()
        endOfFirstLinePos = self.findEOL()
        lineCol = self.getRowCol()
        self.getDirectiveStartToken()
        if directiveName not in 'else elif for while try except finally'.split():
            self.advance(len(directiveName))

        self.getWhiteSpace()

        expr = self.getExpression(pyTokensToBreakAt=[':'])
        if self.matchColonForSingleLineShortFormDirective():
            self.advance()  # skip over :
            if directiveName in 'else elif except finally'.split():
                callback(expr, dedent=False, lineCol=lineCol)
            else:
                callback(expr, lineCol=lineCol)

            self.getWhiteSpace(max=1)
            self.parse(breakPoint=self.findEOL(gobble=True))
            self._compiler.commitStrConst()
            self._compiler.dedent()
        else:
            if self.peek() == ':':
                self.advance()
            self.getWhiteSpace()
            self._eatRestOfDirectiveTag(isLineClearToStartToken, endOfFirstLinePos)
            if directiveName in self._closeableDirectives:
                self.pushToOpenDirectivesStack(directiveName)
            callback(expr, lineCol=lineCol)

    def eatEndDirective(self):
        isLineClearToStartToken = self.isLineClearToStartToken()
        self.getDirectiveStartToken()
        self.advance(3)                 # to end of 'end'
        self.getWhiteSpace()
        pos = self.pos()
        directiveName = False
        for key in self._endDirectiveNamesAndHandlers.keys():
            if self.find(key, pos) == pos:
                directiveName = key
                break
        if not directiveName:
            raise ParseError(self, msg='Invalid end directive')

        endOfFirstLinePos = self.findEOL()
        self.getExpression()  # eat in any extra comment-like crap
        self._eatRestOfDirectiveTag(isLineClearToStartToken, endOfFirstLinePos)
        if directiveName in self._closeableDirectives:
            self.popFromOpenDirectivesStack(directiveName)

        # subclasses can override the default behaviours here by providing an
        # end-directive handler in self._endDirectiveNamesAndHandlers[directiveName]
        if self._endDirectiveNamesAndHandlers.get(directiveName):
            handler = self._endDirectiveNamesAndHandlers[directiveName]
            handler()
        elif directiveName in 'block call filter'.split():
            if key == 'block':
                self._compiler.closeBlock()
            elif key == 'call':
                self._compiler.endCallRegion()
            elif key == 'filter':
                self._compiler.closeFilterBlock()
        elif directiveName in 'while for if try'.split():
            self._compiler.commitStrConst()
            self._compiler.dedent()

    # specific directive eat methods

    def eatEncoding(self):
        self.getDirectiveStartToken()
        self.advance(len('encoding'))
        self.getWhiteSpace()
        encoding = self.readToEOL()
        self._compiler.setModuleEncoding(encoding.strip())

    def eatCompilerSettings(self):
        isLineClearToStartToken = self.isLineClearToStartToken()
        endOfFirstLine = self.findEOL()
        self.getDirectiveStartToken()
        self.advance(len('compiler-settings'))   # to end of 'settings'

        keywords = self.getTargetVarsList()
        self.getExpression()            # gobble any garbage

        self._eatRestOfDirectiveTag(isLineClearToStartToken, endOfFirstLine)

        settingsStr = self._eatToThisEndDirective('compiler-settings')
        try:
            self._compiler.setCompilerSettings(keywords=keywords, settingsStr=settingsStr)
        except:
            sys.stderr.write('An error occurred while processing the following compiler settings.\n')
            sys.stderr.write('----------------------------------------------------------------------\n')
            sys.stderr.write('%s\n' % settingsStr.strip())
            sys.stderr.write('----------------------------------------------------------------------\n')
            sys.stderr.write('Please check the syntax of these settings.\n\n')
            raise

    def eatAttr(self):
        isLineClearToStartToken = self.isLineClearToStartToken()
        endOfFirstLinePos = self.findEOL()
        self.getDirectiveStartToken()
        self.advance(len('attr'))
        self.getWhiteSpace()
        if self.matchCheetahVarStart():
            self.getCheetahVarStartToken()
        attribName = self.getIdentifier()
        self.getWhiteSpace()
        self.getAssignmentOperator()
        expr = self.getExpression()
        self._compiler.addAttribute(attribName, expr)
        self._eatRestOfDirectiveTag(isLineClearToStartToken, endOfFirstLinePos)

    def eatDecorator(self):
        isLineClearToStartToken = self.isLineClearToStartToken()
        endOfFirstLinePos = self.findEOL()
        self.getDirectiveStartToken()
        # self.advance()  # eat @
        decoratorExpr = self.getExpression()
        self._compiler.addDecorator(decoratorExpr)
        self._eatRestOfDirectiveTag(isLineClearToStartToken, endOfFirstLinePos)
        self.getWhiteSpace()

        directiveName = self.matchDirective()
        if not directiveName or directiveName not in ('def', 'block', '@'):
            raise ParseError(
                self, msg='Expected #def, #block or another @decorator')
        self.eatDirective()

    def eatDef(self):
        self._eatDefOrBlock('def')

    def eatBlock(self):
        startPos = self.pos()
        methodName, rawSignature = self._eatDefOrBlock('block')
        self._compiler._blockMetaData[methodName] = {
            'raw': rawSignature,
            'lineCol': self.getRowCol(startPos),
            }

    def _eatDefOrBlock(self, directiveName):
        assert directiveName in ('def', 'block')
        isLineClearToStartToken = self.isLineClearToStartToken()
        endOfFirstLinePos = self.findEOL()
        startPos = self.pos()
        self.getDirectiveStartToken()
        self.advance(len(directiveName))
        self.getWhiteSpace()
        if self.matchCheetahVarStart():
            self.getCheetahVarStartToken()
        methodName = self.getIdentifier()
        self.getWhiteSpace()
        if self.peek() == '(':
            argsList = self.getDefArgList()
            self.advance()              # past the closing ')'
            if argsList and argsList[0][0] == 'self':
                del argsList[0]
        else:
            argsList = []

        if self.matchColonForSingleLineShortFormDirective():
            isNestedDef = (self.setting('allowNestedDefScopes')
                           and [name for name in self._openDirectivesStack if name == 'def'])
            self.getc()
            rawSignature = self[startPos:endOfFirstLinePos]
            self._eatSingleLineDef(directiveName=directiveName,
                                   methodName=methodName,
                                   argsList=argsList,
                                   startPos=startPos,
                                   endPos=endOfFirstLinePos)
            if directiveName == 'def' and not isNestedDef:
                # @@TR: must come before _eatRestOfDirectiveTag ... for some reason
                self._compiler.closeDef()
            elif directiveName == 'block':
                self._compiler.closeBlock()
            elif isNestedDef:
                self._compiler.dedent()

            self._eatRestOfDirectiveTag(isLineClearToStartToken, endOfFirstLinePos)
        else:
            if self.peek() == ':':
                self.getc()
            self.pushToOpenDirectivesStack(directiveName)
            rawSignature = self[startPos:self.pos()]
            self._eatMultiLineDef(directiveName=directiveName,
                                  methodName=methodName,
                                  argsList=argsList,
                                  startPos=startPos,
                                  isLineClearToStartToken=isLineClearToStartToken)

        return methodName, rawSignature

    def _eatMultiLineDef(self, directiveName, methodName, argsList, startPos,
                         isLineClearToStartToken=False):
        self.getExpression()            # slurp up any garbage left at the end
        signature = self[startPos:self.pos()]
        endOfFirstLinePos = self.findEOL()
        self._eatRestOfDirectiveTag(isLineClearToStartToken, endOfFirstLinePos)
        signature = ' '.join([line.strip() for line in signature.splitlines()])
        parserComment = ('## CHEETAH: generated from ' + signature +
                         ' at line %s, col %s' % self.getRowCol(startPos)
                         + '.')

        isNestedDef = (self.setting('allowNestedDefScopes')
                       and len([name for name in self._openDirectivesStack if name == 'def']) > 1)
        if directiveName == 'block' or (directiveName == 'def' and not isNestedDef):
            self._compiler.startMethodDef(methodName, argsList, parserComment)
        else:  # nested def
            self._useSearchList_orig = self.setting('useSearchList')
            self.setSetting('useSearchList', False)
            self._compiler.addClosure(methodName, argsList, parserComment)

        return methodName

    def _eatSingleLineDef(self, directiveName, methodName, argsList, startPos, endPos):
        fullSignature = self[startPos:endPos]
        parserComment = ('## Generated from ' + fullSignature +
                         ' at line %s, col %s' % self.getRowCol(startPos)
                         + '.')
        isNestedDef = (self.setting('allowNestedDefScopes')
                       and [name for name in self._openDirectivesStack if name == 'def'])
        if directiveName == 'block' or (directiveName == 'def' and not isNestedDef):
            self._compiler.startMethodDef(methodName, argsList, parserComment)
        else:  # nested def
            # @@TR: temporary hack of useSearchList
            useSearchList_orig = self.setting('useSearchList')
            self.setSetting('useSearchList', False)
            self._compiler.addClosure(methodName, argsList, parserComment)

        self.getWhiteSpace(max=1)
        self.parse(breakPoint=endPos)
        if isNestedDef:  # @@TR: temporary hack of useSearchList
            self.setSetting('useSearchList', useSearchList_orig)

    def eatExtends(self):
        isLineClearToStartToken = self.isLineClearToStartToken()
        endOfFirstLine = self.findEOL()
        self.getDirectiveStartToken()
        self.advance(len('extends'))
        self.getWhiteSpace()
        basecls_name = self.readToEOL(gobble=False)

        if ',' in basecls_name:
            raise ParseError(
                self, 'yelp_cheetah does not support multiple inheritance'
            )

        self._compiler.setBaseClass(basecls_name)
        self._eatRestOfDirectiveTag(isLineClearToStartToken, endOfFirstLine)

    def eatImplements(self):
        isLineClearToStartToken = self.isLineClearToStartToken()
        endOfFirstLine = self.findEOL()
        self.getDirectiveStartToken()
        self.advance(len('implements'))
        self.getWhiteSpace()
        methodName = self.getIdentifier()
        if not self.atEnd() and self.peek() == '(':
            raise ParseError(
                self, 'yelp_cheetah does not support argspecs for #implements',
            )
        self._compiler.setMainMethodName(methodName)

        self.getExpression()  # throw away and unwanted crap that got added in
        self._eatRestOfDirectiveTag(isLineClearToStartToken, endOfFirstLine)

    def eatSuper(self):
        isLineClearToStartToken = self.isLineClearToStartToken()
        endOfFirstLine = self.findEOL()
        self.getDirectiveStartToken()
        self.advance(len('super'))
        self.getWhiteSpace()
        if not self.atEnd() and self.peek() == '(':
            argsList = self.getDefArgList()
            self.advance()              # past the closing ')'
            if argsList and argsList[0][0] == 'self':
                del argsList[0]
        else:
            argsList = []

        # parserComment = ('## CHEETAH: generated from ' + signature +
        #                 ' at line %s, col %s' % self.getRowCol(startPos)
        #                 + '.')

        self.getExpression()  # throw away and unwanted crap that got added in
        self._eatRestOfDirectiveTag(isLineClearToStartToken, endOfFirstLine)
        self._compiler.addSuper(argsList)

    def eatSet(self):
        isLineClearToStartToken = self.isLineClearToStartToken()
        endOfFirstLine = self.findEOL()
        self.getDirectiveStartToken()
        self.advance(3)
        self.getWhiteSpace()
        style = SET_LOCAL
        if self.startswith('global'):
            self.getIdentifier()
            self.getWhiteSpace()
            style = SET_GLOBAL
        elif self.startswith('module'):
            self.getIdentifier()
            self.getWhiteSpace()
            style = SET_MODULE

        LVALUE = self.getExpression(pyTokensToBreakAt=assignmentOps, useNameMapper=False).strip()
        OP = self.getAssignmentOperator()
        RVALUE = self.getExpression()
        expr = LVALUE + ' ' + OP + ' ' + RVALUE.strip()

        self._eatRestOfDirectiveTag(isLineClearToStartToken, endOfFirstLine)

        # used for 'set global'
        class Components:
            pass
        exprComponents = Components()
        exprComponents.LVALUE = LVALUE
        exprComponents.OP = OP
        exprComponents.RVALUE = RVALUE
        self._compiler.addSet(expr, exprComponents, style)

    def eatSlurp(self):
        if self.isLineClearToStartToken():
            self._compiler.handleWSBeforeDirective()
        self._compiler.commitStrConst()
        self.readToEOL(gobble=True)

    def eatMacroCall(self):
        isLineClearToStartToken = self.isLineClearToStartToken()
        endOfFirstLinePos = self.findEOL()
        startPos = self.pos()
        self.getDirectiveStartToken()
        macroName = self.getIdentifier()
        macro = self._macros[macroName]
        if hasattr(macro, 'parse'):
            return macro.parse(parser=self, startPos=startPos)

        if hasattr(macro, 'parseArgs'):
            args = macro.parseArgs(parser=self, startPos=startPos)
        else:
            self.getWhiteSpace()
            args = self.getExpression(useNameMapper=False,
                                      pyTokensToBreakAt=[':']).strip()

        if self.matchColonForSingleLineShortFormDirective():
            isShortForm = True
            self.advance()  # skip over :
            self.getWhiteSpace(max=1)
            srcBlock = self.readToEOL(gobble=False)
            EOLCharsInShortForm = self.readToEOL(gobble=True)
            # self.readToEOL(gobble=False)
        else:
            isShortForm = False
            if self.peek() == ':':
                self.advance()
            self.getWhiteSpace()
            self._eatRestOfDirectiveTag(isLineClearToStartToken, endOfFirstLinePos)
            srcBlock = self._eatToThisEndDirective(macroName)

        if hasattr(macro, 'convertArgStrToDict'):
            kwArgs = macro.convertArgStrToDict(args, parser=self, startPos=startPos)
        else:
            def getArgs(*pargs, **kws):
                return pargs, kws
            exec('positionalArgs, kwArgs = getArgs(%(args)s)' % locals())

        assert 'src' not in kwArgs
        kwArgs['src'] = srcBlock

        if isinstance(macro, types.MethodType):
            co = macro.im_func.func_code
        elif (hasattr(macro, '__call__')
              and hasattr(macro.__call__, 'im_func')):
            co = macro.__call__.im_func.func_code
        else:
            co = macro.func_code
        availableKwArgs = inspect.getargs(co)[0]

        if 'parser' in availableKwArgs:
            kwArgs['parser'] = self
        if 'macros' in availableKwArgs:
            kwArgs['macros'] = self._macros
        if 'compilerSettings' in availableKwArgs:
            kwArgs['compilerSettings'] = self.settings()
        if 'isShortForm' in availableKwArgs:
            kwArgs['isShortForm'] = isShortForm
        if isShortForm and 'EOLCharsInShortForm' in availableKwArgs:
            kwArgs['EOLCharsInShortForm'] = EOLCharsInShortForm

        if 'startPos' in availableKwArgs:
            kwArgs['startPos'] = startPos
        if 'endPos' in availableKwArgs:
            kwArgs['endPos'] = self.pos()

        srcFromMacroOutput = macro(**kwArgs)

        origParseSrc = self._src
        origBreakPoint = self.breakPoint()
        origPos = self.pos()
        # add a comment to the output about the macro src that is being parsed
        # or add a comment prefix to all the comments added by the compiler
        self._src = srcFromMacroOutput
        self.setPos(0)
        self.setBreakPoint(len(srcFromMacroOutput))

        self.parse(assertEmptyStack=False)

        self._src = origParseSrc
        self.setBreakPoint(origBreakPoint)
        self.setPos(origPos)

    def eatCall(self):
        # @@TR: need to enable single line version of this
        isLineClearToStartToken = self.isLineClearToStartToken()
        endOfFirstLinePos = self.findEOL()
        lineCol = self.getRowCol()
        self.getDirectiveStartToken()
        self.advance(len('call'))

        useAutocallingOrig = self.setting('useAutocalling')
        self.setSetting('useAutocalling', False)
        self.getWhiteSpace()
        if self.matchCheetahVarStart():
            functionName = self.getCheetahVar()
        else:
            functionName = self.getCheetahVar(plain=True, skipStartToken=True)
        self.setSetting('useAutocalling', useAutocallingOrig)

        self.getWhiteSpace()
        args = self.getExpression(pyTokensToBreakAt=[':']).strip()
        if self.matchColonForSingleLineShortFormDirective():
            self.advance()  # skip over :
            self._compiler.startCallRegion(functionName, args, lineCol)
            self.getWhiteSpace(max=1)
            self.parse(breakPoint=self.findEOL(gobble=False))
            self._compiler.endCallRegion()
        else:
            if self.peek() == ':':
                self.advance()
            self.getWhiteSpace()
            self.pushToOpenDirectivesStack("call")
            self._eatRestOfDirectiveTag(isLineClearToStartToken, endOfFirstLinePos)
            self._compiler.startCallRegion(functionName, args, lineCol)

    def eatFilter(self):
        isLineClearToStartToken = self.isLineClearToStartToken()
        endOfFirstLinePos = self.findEOL()

        self.getDirectiveStartToken()
        self.advance(len('filter'))
        self.getWhiteSpace()
        if self.matchCheetahVarStart():
            isKlass = True
            theFilter = self.getExpression(pyTokensToBreakAt=[':'])
        else:
            isKlass = False
            theFilter = self.getIdentifier()
            self.getWhiteSpace()

        if self.matchColonForSingleLineShortFormDirective():
            self.advance()  # skip over :
            self.getWhiteSpace(max=1)
            self._compiler.setFilter(theFilter, isKlass)
            self.parse(breakPoint=self.findEOL(gobble=False))
            self._compiler.closeFilterBlock()
        else:
            if self.peek() == ':':
                self.advance()
            self.getWhiteSpace()
            self.pushToOpenDirectivesStack("filter")
            self._eatRestOfDirectiveTag(isLineClearToStartToken, endOfFirstLinePos)
            self._compiler.setFilter(theFilter, isKlass)

    def eatIf(self):
        isLineClearToStartToken = self.isLineClearToStartToken()
        endOfFirstLine = self.findEOL()
        lineCol = self.getRowCol()
        self.getDirectiveStartToken()

        expressionParts = self.getExpressionParts(pyTokensToBreakAt=[':'])
        expr = ''.join(expressionParts).strip()

        isTernaryExpr = ('then' in expressionParts and 'else' in expressionParts)
        if isTernaryExpr:
            conditionExpr = []
            trueExpr = []
            falseExpr = []
            currentExpr = conditionExpr
            for part in expressionParts:
                if part.strip() == 'then':
                    currentExpr = trueExpr
                elif part.strip() == 'else':
                    currentExpr = falseExpr
                else:
                    currentExpr.append(part)

            conditionExpr = ''.join(conditionExpr)
            trueExpr = ''.join(trueExpr)
            falseExpr = ''.join(falseExpr)
            self._eatRestOfDirectiveTag(isLineClearToStartToken, endOfFirstLine)
            self._compiler.addTernaryExpr(conditionExpr, trueExpr, falseExpr, lineCol=lineCol)
        elif self.matchColonForSingleLineShortFormDirective():
            self.advance()  # skip over :
            self._compiler.addIf(expr, lineCol=lineCol)
            self.getWhiteSpace(max=1)
            self.parse(breakPoint=self.findEOL(gobble=True))
            self._compiler.commitStrConst()
            self._compiler.dedent()
        else:
            if self.peek() == ':':
                self.advance()
            self.getWhiteSpace()
            self._eatRestOfDirectiveTag(isLineClearToStartToken, endOfFirstLine)
            self.pushToOpenDirectivesStack('if')
            self._compiler.addIf(expr, lineCol=lineCol)

    # end directive handlers
    def handleEndDef(self):
        isNestedDef = (self.setting('allowNestedDefScopes')
                       and [name for name in self._openDirectivesStack if name == 'def'])
        if not isNestedDef:
            self._compiler.closeDef()
        else:
            # @@TR: temporary hack of useSearchList
            self.setSetting('useSearchList', self._useSearchList_orig)
            self._compiler.commitStrConst()
            self._compiler.dedent()

    def pushToOpenDirectivesStack(self, directiveName):
        assert directiveName in self._closeableDirectives
        self._openDirectivesStack.append(directiveName)

    def popFromOpenDirectivesStack(self, directiveName):
        if not self._openDirectivesStack:
            raise ParseError(self, msg="#end found, but nothing to end")

        if self._openDirectivesStack[-1] == directiveName:
            del self._openDirectivesStack[-1]
        else:
            raise ParseError(self, msg="#end %s found, expected #end %s" % (
                             directiveName, self._openDirectivesStack[-1]))

    def assertEmptyOpenDirectivesStack(self):
        if self._openDirectivesStack:
            errorMsg = (
                "Some #directives are missing their corresponding #end ___ tag: %s" % (
                    ', '.join(self._openDirectivesStack)))
            raise ParseError(self, msg=errorMsg)