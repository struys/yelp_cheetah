'''
    Compiler classes for Cheetah:
    Compiler
    ClassCompiler
    MethodCompiler

    If you are trying to grok this code start with Compiler.__init__,
    Compiler.compile, and Compiler.__getattr__.
'''

import os
import os.path
import re
import textwrap
import time
import warnings
import copy

from Cheetah import five
from Cheetah.SettingsManager import SettingsManager
from Cheetah.Parser import Parser, ParseError
from Cheetah.Parser import SET_GLOBAL, SET_MODULE
from Cheetah.Parser import escapedNewlineRE


# Settings format: (key, default, docstring)
_DEFAULT_COMPILER_SETTINGS = [
    ('useNameMapper', True, 'Enable NameMapper for dotted notation and searchList support'),
    (
        'useSearchList',
        True,
        (
            'Enable the searchList, requires useNameMapper=True, if disabled, '
            'first portion of the $variable is a global, builtin, or local '
            "variable that doesn't need looking up in the searchList"
        ),
    ),
    ('allowSearchListAsMethArg', True, ''),
    ('useAutocalling', False, 'Detect and call callable objects in searchList, requires useNameMapper=True'),
    ('useDottedNotation', True, 'Allow use of dotted notation for dictionary lookups, requires useNameMapper=True'),
    ('alwaysFilterNone', True, 'Filter out None prior to calling the #filter'),
    ('useLegacyImportMode', True, 'All #import statements are relocated to the top of the generated Python module'),
    (
        'prioritizeSearchListOverSelf',
        False,
        (
            'When iterating the searchList, look into the searchList passed '
            'into the initializer instead of Template members first'
        ),
    ),
    ('autoAssignDummyTransactionToSelf', False, ''),
    ('useKWsDictArgForPassingTrans', True, ''),

    ('commentOffset', 1, ''),
    ('mainMethodName', 'respond', ''),
    ('mainMethodNameForSubclasses', 'writeBody', ''),
    ('indentationStep', ' ' * 4, ''),
    ('initialMethIndentLevel', 2, ''),

    # Customizing the #extends directive
    ('autoImportForExtendsDirective', True, ''),

    ('cheetahVarStartToken', '$', ''),
    ('commentStartToken', '##', ''),
    ('directiveStartToken', '#', ''),
    ('directiveEndToken', '#', ''),
    ('PSPStartToken', '<%', ''),
    ('PSPEndToken', '%>', ''),
    ('gettextTokens', ["_", "N_", "ngettext"], ''),
    ('allowNestedDefScopes', True, ''),
    ('macroDirectives', {}, 'For providing macros'),
]

DEFAULT_COMPILER_SETTINGS = dict([(v[0], v[1]) for v in _DEFAULT_COMPILER_SETTINGS])


class GenUtils(object):
    """An abstract baseclass for the Compiler classes that provides methods that
    perform generic utility functions or generate pieces of output code from
    information passed in by the Parser baseclass.  These methods don't do any
    parsing themselves.
    """

    def genCheetahVar(self, nameChunks, plain=False):
        if nameChunks[0][0] in self.setting('gettextTokens'):
            self.addGetTextVar(nameChunks)
        if self.setting('useNameMapper') and not plain:
            return self.genNameMapperVar(nameChunks)
        else:
            return self.genPlainVar(nameChunks)

    def addGetTextVar(self, nameChunks):
        """Output something that gettext can recognize.

        This is a harmless side effect necessary to make gettext work when it
        is scanning compiled templates for strings marked for translation.

        @@TR: another marginally more efficient approach would be to put the
        output in a dummy method that is never called.
        """
        # @@TR: this should be in the compiler not here
        self.addChunk("if False:")
        self.indent()
        self.addChunk(self.genPlainVar(nameChunks[:]))
        self.dedent()

    def genPlainVar(self, nameChunks):
        """Generate Python code for a Cheetah $var without using NameMapper
        (Unified Dotted Notation with the SearchList).
        """
        nameChunks.reverse()
        chunk = nameChunks.pop()
        pythonCode = chunk[0] + chunk[2]
        while nameChunks:
            chunk = nameChunks.pop()
            pythonCode = (pythonCode + '.' + chunk[0] + chunk[2])
        return pythonCode

    def genNameMapperVar(self, nameChunks):
        """Generate valid Python code for a Cheetah $var, using NameMapper
        (Unified Dotted Notation with the SearchList).

        nameChunks = list of var subcomponents represented as tuples
          [ (name,useAC,remainderOfExpr),
          ]
        where:
          name = the dotted name base
          useAC = where NameMapper should use autocalling on namemapperPart
          remainderOfExpr = any arglist, index, or slice

        If remainderOfExpr contains a call arglist (e.g. '(1234)') then useAC
        is False, otherwise it defaults to True. It is overridden by the global
        setting 'useAutocalling' if this setting is False.

        EXAMPLE
        ------------------------------------------------------------------------
        if the raw Cheetah Var is
          $a.b.c[1].d().x.y.z

        nameChunks is the list
          [ ('a.b.c',True,'[1]'), # A
            ('d',False,'()'),     # B
            ('x.y.z',True,''),    # C
          ]

        When this method is fed the list above it returns
          VFN(VFN(VFFSL(SL, 'a.b.c',True)[1], 'd',False)(), 'x.y.z',True)
        which can be represented as
          VFN(B`, name=C[0], executeCallables=(useAC and C[1]))C[2]
        where:
          VFN = NameMapper.valueForName
          VFFSL = NameMapper.valueFromFrameOrSearchList
          VFSL = NameMapper.valueFromSearchList # optionally used instead of VFFSL
          SL = self.searchList()
          useAC = self.setting('useAutocalling') # True in this example

          A = ('a.b.c',True,'[1]')
          B = ('d',False,'()')
          C = ('x.y.z',True,'')

          C` = VFN( VFN( VFFSL(SL, 'a.b.c',True)[1],
                         'd',False)(),
                    'x.y.z',True)
             = VFN(B`, name='x.y.z', executeCallables=True)

          B` = VFN(A`, name=B[0], executeCallables=(useAC and B[1]))B[2]
          A` = VFFSL(SL, name=A[0], executeCallables=(useAC and A[1]))A[2]
        """
        defaultUseAC = self.setting('useAutocalling')
        useDottedNotation = self.setting('useDottedNotation')
        useSearchList = self.setting('useSearchList')

        nameChunks.reverse()
        name, useAC, remainder = nameChunks.pop()

        if not useSearchList:
            firstDotIdx = name.find('.')
            if firstDotIdx != -1 and firstDotIdx < len(name):
                beforeFirstDot, afterDot = name[:firstDotIdx], name[firstDotIdx + 1:]
                pythonCode = 'VFN(%s, "%s", %s, %s)%s' % (
                    beforeFirstDot,
                    afterDot,
                    defaultUseAC and useAC,
                    useDottedNotation,
                    remainder,
                )
            else:
                pythonCode = name + remainder
        else:
            pythonCode = 'VFFSL(SL, "%s", %s, %s)%s' % (
                name,
                defaultUseAC and useAC,
                useDottedNotation,
                remainder,
            )

        while nameChunks:
            name, useAC, remainder = nameChunks.pop()
            pythonCode = 'VFN(%s, "%s", %s, %s)%s' % (
                pythonCode,
                name,
                defaultUseAC and useAC,
                useDottedNotation,
                remainder,
            )

        return pythonCode

##################################################
# METHOD COMPILERS


class MethodCompiler(GenUtils):
    def __init__(self, methodName, classCompiler,
                 initialMethodComment=None,
                 decorators=None):
        self._next_variable_id = 0
        self._settingsManager = classCompiler
        self._classCompiler = classCompiler
        self._moduleCompiler = classCompiler._moduleCompiler
        self._methodName = methodName
        self._initialMethodComment = initialMethodComment
        self._setupState()
        self._decorators = decorators or []

    def setting(self, key):
        return self._settingsManager.setting(key)

    def _setupState(self):
        self._indent = self.setting('indentationStep')
        self._indentLev = self.setting('initialMethIndentLevel')
        self._pendingStrConstChunks = []
        self._methodSignature = None
        self._methodBodyChunks = []

        self._callRegionsStack = []
        self._filterRegionsStack = []

        self._hasReturnStatement = False
        self._isGenerator = False

    def cleanupState(self):
        """Called by the containing class compiler instance
        """
        pass

    def methodName(self):
        return self._methodName

    def setMethodName(self, name):
        self._methodName = name

    # methods for managing indentation

    def indentation(self):
        return self._indent * self._indentLev

    def indent(self):
        self._indentLev += 1

    def dedent(self):
        if not self._indentLev:
            raise AssertionError('Attempt to dedent when the indentLev is 0')
        self._indentLev -= 1

    # methods for final code wrapping

    def methodDef(self):
        self.commitStrConst()
        methodDefChunks = (
            self.methodSignature(),
            '\n',
            self.methodBody())
        methodDef = ''.join(methodDefChunks)
        return methodDef

    def methodSignature(self):
        return self._indent + self._methodSignature + ':'

    def setMethodSignature(self, signature):
        self._methodSignature = signature

    def methodBody(self):
        return ''.join(self._methodBodyChunks)

    # methods for adding code

    def addChunk(self, chunk):
        self.commitStrConst()
        chunk = "\n" + self.indentation() + chunk
        self._methodBodyChunks.append(chunk)

    def appendToPrevChunk(self, appendage):
        self._methodBodyChunks[-1] = self._methodBodyChunks[-1] + appendage

    def addWriteChunk(self, chunk):
        self.addChunk('write(' + chunk + ')')

    def addFilteredChunk(self, chunk, filterArgs=None, rawExpr=None, lineCol=None):
        if filterArgs is None:
            filterArgs = ''

        if self.setting('alwaysFilterNone'):
            if rawExpr and rawExpr.find('\n') == -1 and rawExpr.find('\r') == -1:
                self.addChunk("_v = %s # %r" % (chunk, rawExpr))
                if lineCol:
                    self.appendToPrevChunk(' on line %s, col %s' % lineCol)
            else:
                self.addChunk("_v = %s" % chunk)

            self.addChunk("if _v is not NO_CONTENT: write(_filter(_v%s))" % filterArgs)
        else:
            self.addChunk("write(_filter(%s%s))" % (chunk, filterArgs))

    def _appendToPrevStrConst(self, strConst):
        if self._pendingStrConstChunks:
            self._pendingStrConstChunks.append(strConst)
        else:
            self._pendingStrConstChunks = [strConst]

    def commitStrConst(self):
        """Add the code for outputting the pending strConst without chopping off
        any whitespace from it.
        """
        if not self._pendingStrConstChunks:
            return

        strConst = ''.join(self._pendingStrConstChunks)
        self._pendingStrConstChunks = []
        if not strConst:
            return

        reprstr = repr(strConst)
        i = 0
        out = []
        if reprstr.startswith('u'):
            i = 1
            out = ['u']
        body = escapedNewlineRE.sub('\\1\n', reprstr[i+1:-1])

        if reprstr[i] == "'":
            out.append("'''")
            out.append(body)
            out.append("'''")
        else:
            out.append('"""')
            out.append(body)
            out.append('"""')
        self.addWriteChunk(''.join(out))

    def handleWSBeforeDirective(self):
        """Truncate the pending strCont to the beginning of the current line.
        """
        if self._pendingStrConstChunks:
            src = self._pendingStrConstChunks[-1]
            BOL = max(src.rfind('\n') + 1, src.rfind('\r') + 1, 0)
            if BOL < len(src):
                self._pendingStrConstChunks[-1] = src[:BOL]

    # @@TR: consider merging the next two methods into one
    def addStrConst(self, strConst):
        self._appendToPrevStrConst(strConst)

    def addMethComment(self, comm):
        offSet = self.setting('commentOffset')
        self.addChunk('#' + ' ' * offSet + comm)

    def addPlaceholder(self, expr, filterArgs, rawPlaceholder, lineCol):
        self.addFilteredChunk(expr, filterArgs, rawPlaceholder, lineCol=lineCol)
        self.appendToPrevChunk(' # from line %s, col %s' % lineCol + '.')

    def addSilent(self, expr):
        self.addChunk(expr)

    def addSet(self, expr, exprComponents, setStyle):
        if setStyle is SET_GLOBAL:
            (LVALUE, OP, RVALUE) = (exprComponents.LVALUE,
                                    exprComponents.OP,
                                    exprComponents.RVALUE)
            # we need to split the LVALUE to deal with globalSetVars
            splitPos1 = LVALUE.find('.')
            splitPos2 = LVALUE.find('[')
            if splitPos1 > 0 and splitPos2 == -1:
                splitPos = splitPos1
            elif splitPos1 > 0 and splitPos1 < max(splitPos2, 0):
                splitPos = splitPos1
            else:
                splitPos = splitPos2

            if splitPos > 0:
                primary = LVALUE[:splitPos]
                secondary = LVALUE[splitPos:]
            else:
                primary = LVALUE
                secondary = ''
            LVALUE = 'self._CHEETAH__globalSetVars["' + primary + '"]' + secondary
            expr = LVALUE + ' ' + OP + ' ' + RVALUE.strip()

        if setStyle is SET_MODULE:
            self._moduleCompiler.addModuleGlobal(expr)
        else:
            self.addChunk(expr)

    def addWhile(self, expr, lineCol=None):
        self.addIndentingDirective(expr, lineCol=lineCol)

    def addFor(self, expr, lineCol=None):
        self.addIndentingDirective(expr, lineCol=lineCol)

    def addIndentingDirective(self, expr, lineCol=None):
        if expr and not expr[-1] == ':':
            expr = expr + ':'
        self.addChunk(expr)
        if lineCol:
            self.appendToPrevChunk(' # generated from line %s, col %s' % lineCol)
        self.indent()

    def addReIndentingDirective(self, expr, dedent=True, lineCol=None):
        self.commitStrConst()
        if dedent:
            self.dedent()
        if not expr[-1] == ':':
            expr = expr + ':'

        self.addChunk(expr)
        if lineCol:
            self.appendToPrevChunk(' # generated from line %s, col %s' % lineCol)
        self.indent()

    def addIf(self, expr, lineCol=None):
        """For a full #if ... #end if directive
        """
        self.addIndentingDirective(expr, lineCol=lineCol)

    def addTernaryExpr(self, conditionExpr, trueExpr, falseExpr, lineCol=None):
        """For a single-lie #if ... then .... else ... directive
        <condition> then <trueExpr> else <falseExpr>
        """
        self.addIndentingDirective(conditionExpr, lineCol=lineCol)
        self.addFilteredChunk(trueExpr)
        self.dedent()
        self.addIndentingDirective('else')
        self.addFilteredChunk(falseExpr)
        self.dedent()

    def addElse(self, expr, dedent=True, lineCol=None):
        expr = re.sub(r'else[ \f\t]+if', 'elif', expr)
        self.addReIndentingDirective(expr, dedent=dedent, lineCol=lineCol)

    def addElif(self, expr, dedent=True, lineCol=None):
        self.addElse(expr, dedent=dedent, lineCol=lineCol)

    def addClosure(self, functionName, argsList, parserComment):
        argStringChunks = []
        for arg in argsList:
            chunk = arg[0]
            if arg[1] is not None:
                chunk += '=' + arg[1]
            argStringChunks.append(chunk)
        signature = "def " + functionName + "(" + ','.join(argStringChunks) + "):"
        self.addIndentingDirective(signature)
        self.addChunk('#' + parserComment)

    def addTry(self, expr, lineCol=None):
        self.addIndentingDirective(expr, lineCol=lineCol)

    def addExcept(self, expr, dedent=True, lineCol=None):
        self.addReIndentingDirective(expr, dedent=dedent, lineCol=lineCol)

    def addFinally(self, expr, dedent=True, lineCol=None):
        self.addReIndentingDirective(expr, dedent=dedent, lineCol=lineCol)

    def addReturn(self, expr):
        assert not self._isGenerator
        self.addChunk(expr)
        self._hasReturnStatement = True

    def addYield(self, expr):
        assert not self._hasReturnStatement
        self._isGenerator = True
        if expr.replace('yield', '').strip():
            self.addChunk(expr)
        else:
            self.addChunk('if _dummyTrans:')
            self.indent()
            self.addChunk('yield trans.response().getvalue()')
            self.addChunk('trans = DummyTransaction()')
            self.addChunk('write = trans.response().write')
            self.dedent()
            self.addChunk('else:')
            self.indent()
            self.addChunk(
                'raise TypeError("This method cannot be called with a trans arg")')
            self.dedent()

    def addPass(self, expr):
        self.addChunk(expr)

    def addDel(self, expr):
        self.addChunk(expr)

    def addAssert(self, expr):
        self.addChunk(expr)

    def addRaise(self, expr):
        self.addChunk(expr)

    def addBreak(self, expr):
        self.addChunk(expr)

    def addContinue(self, expr):
        self.addChunk(expr)

    def addPSP(self, PSP):
        self.commitStrConst()

        for line in PSP.splitlines():
            self.addChunk(line)

    def nextCacheID(self):
        self._next_variable_id += 1
        return u'_{0}'.format(self._next_variable_id)

    def nextCallRegionID(self):
        return self.nextCacheID()

    def startCallRegion(self, functionName, args, lineCol, regionTitle='CALL'):
        class CallDetails(object):
            pass
        callDetails = CallDetails()
        callDetails.ID = ID = self.nextCallRegionID()
        callDetails.functionName = functionName
        callDetails.args = args
        callDetails.lineCol = lineCol
        self._callRegionsStack.append((ID, callDetails))  # attrib of current methodCompiler

        self.addChunk('## START %(regionTitle)s REGION: ' % locals()
                      + ID
                      + ' of ' + functionName
                      + ' at line %s, col %s' % lineCol + ' in the source.')
        self.addChunk('_orig_trans%(ID)s = trans' % locals())
        self.addChunk('_wasBuffering%(ID)s = self._CHEETAH__isBuffering' % locals())
        self.addChunk('trans = _callCollector%(ID)s = DummyTransaction()' % locals())
        if self.setting('autoAssignDummyTransactionToSelf'):
            self.addChunk('self.transaction = trans')
        else:
            self.addChunk('self._CHEETAH__isBuffering = True')
        self.addChunk('write = _callCollector%(ID)s.response().write' % locals())

    def endCallRegion(self, regionTitle='CALL'):
        ID, callDetails = self._callRegionsStack[-1]
        functionName, initialKwArgs, lineCol = (
            callDetails.functionName, callDetails.args, callDetails.lineCol)

        def reset(ID=ID):
            self.addChunk('trans = _orig_trans%(ID)s' % locals())
            if self.setting('autoAssignDummyTransactionToSelf'):
                self.addChunk('self.transaction = trans')
            self.addChunk('write = trans.response().write')
            self.addChunk('self._CHEETAH__isBuffering = _wasBuffering%(ID)s ' % locals())
            self.addChunk('del _wasBuffering%(ID)s' % locals())
            self.addChunk('del _orig_trans%(ID)s' % locals())

        reset()
        self.addChunk('_callArgVal%(ID)s = _callCollector%(ID)s.response().getvalue()' % locals())
        self.addChunk('del _callCollector%(ID)s' % locals())
        if initialKwArgs:
            initialKwArgs = ', ' + initialKwArgs
        self.addFilteredChunk('%(functionName)s(_callArgVal%(ID)s%(initialKwArgs)s)' % locals())
        self.addChunk('del _callArgVal%(ID)s' % locals())
        self.addChunk('## END %(regionTitle)s REGION: ' % locals()
                      + ID
                      + ' of ' + functionName
                      + ' at line %s, col %s' % lineCol + ' in the source.')
        self.addChunk('')
        self._callRegionsStack.pop()  # attrib of current methodCompiler

    def nextFilterRegionID(self):
        return self.nextCacheID()

    def setFilter(self, theFilter, isKlass):
        class FilterDetails:
            pass
        filterDetails = FilterDetails()
        filterDetails.ID = ID = self.nextFilterRegionID()
        filterDetails.theFilter = theFilter
        filterDetails.isKlass = isKlass
        self._filterRegionsStack.append((ID, filterDetails))  # attrib of current methodCompiler

        self.addChunk('_orig_filter%(ID)s = _filter' % locals())
        if isKlass:
            self.addChunk('_filter = self._CHEETAH__currentFilter = ' + theFilter.strip() +
                          '(self).filter')
        else:
            if theFilter.lower() == 'none':
                self.addChunk('_filter = self._CHEETAH__initialFilter')
            else:
                # is string representing the name of a builtin filter
                self.addChunk('filterName = ' + repr(theFilter))
                self.addChunk('if self._CHEETAH__filters.has_key("' + theFilter + '"):')
                self.indent()
                self.addChunk('_filter = self._CHEETAH__currentFilter = self._CHEETAH__filters[filterName]')
                self.dedent()
                self.addChunk('else:')
                self.indent()
                self.addChunk('_filter = self._CHEETAH__currentFilter'
                              + ' = \\\n\t\t\tself._CHEETAH__filters[filterName] = '
                              + 'getattr(self._CHEETAH__filtersLib, filterName)(self).filter')
                self.dedent()

    def closeFilterBlock(self):
        ID, filterDetails = self._filterRegionsStack.pop()
        # self.addChunk('_filter = self._CHEETAH__initialFilter')
        # self.addChunk('_filter = _orig_filter%(ID)s'%locals())
        self.addChunk('_filter = self._CHEETAH__currentFilter = _orig_filter%(ID)s' % locals())


class AutoMethodCompiler(MethodCompiler):

    def _setupState(self):
        MethodCompiler._setupState(self)
        self._argStringList = [("self", None)]
        self._streamingEnabled = True
        self._isClassMethod = None
        self._isStaticMethod = None

    def _useKWsDictArgForPassingTrans(self):
        alreadyHasTransArg = [argname for argname, defval in self._argStringList
                              if argname == 'trans']
        return (self.methodName() != 'respond'
                and not alreadyHasTransArg
                and self.setting('useKWsDictArgForPassingTrans'))

    def isClassMethod(self):
        if self._isClassMethod is None:
            self._isClassMethod = '@classmethod' in self._decorators
        return self._isClassMethod

    def isStaticMethod(self):
        if self._isStaticMethod is None:
            self._isStaticMethod = '@staticmethod' in self._decorators
        return self._isStaticMethod

    def cleanupState(self):
        MethodCompiler.cleanupState(self)
        self.commitStrConst()

        if self._streamingEnabled:
            kwargsName = None
            positionalArgsListName = None
            for argname, defval in self._argStringList:
                if argname.strip().startswith('**'):
                    kwargsName = argname.strip().replace('**', '')
                    break
                elif argname.strip().startswith('*'):
                    positionalArgsListName = argname.strip().replace('*', '')

            if not kwargsName and self._useKWsDictArgForPassingTrans():
                kwargsName = 'KWS'
                self.addMethArg('**KWS', None)
            self._kwargsName = kwargsName

            if not self._useKWsDictArgForPassingTrans():
                if not kwargsName and not positionalArgsListName:
                    self.addMethArg('trans', 'None')
                else:
                    self._streamingEnabled = False

        self._indentLev = self.setting('initialMethIndentLevel')
        mainBodyChunks = self._methodBodyChunks
        self._methodBodyChunks = []
        self._addAutoSetupCode()
        self._methodBodyChunks.extend(mainBodyChunks)
        self._addAutoCleanupCode()

    def _addAutoSetupCode(self):
        if self._initialMethodComment:
            self.addChunk(self._initialMethodComment)

        if self._streamingEnabled and not self.isClassMethod() and not self.isStaticMethod():
            if self._useKWsDictArgForPassingTrans() and self._kwargsName:
                self.addChunk('trans = %s.get("trans")' % self._kwargsName)
            self.addChunk('if (not trans and not self._CHEETAH__isBuffering'
                          ' and not callable(self.transaction)):')
            self.indent()
            self.addChunk('trans = self.transaction'
                          ' # is None unless self.awake() was called')
            self.dedent()
            self.addChunk('if not trans:')
            self.indent()
            self.addChunk('trans = DummyTransaction()')
            if self.setting('autoAssignDummyTransactionToSelf'):
                self.addChunk('self.transaction = trans')
            self.addChunk('_dummyTrans = True')
            self.dedent()
            self.addChunk('else: _dummyTrans = False')
        else:
            self.addChunk('trans = DummyTransaction()')
            self.addChunk('_dummyTrans = True')
        self.addChunk('write = trans.response().write')
        if self.setting('useNameMapper'):
            argNames = [arg[0] for arg in self._argStringList]
            allowSearchListAsMethArg = self.setting('allowSearchListAsMethArg')
            if allowSearchListAsMethArg and 'SL' in argNames:
                pass
            elif allowSearchListAsMethArg and 'searchList' in argNames:
                self.addChunk('SL = searchList')
            elif not self.isClassMethod() and not self.isStaticMethod():
                self.addChunk('SL = self._CHEETAH__searchList')
            else:
                self.addChunk('SL = [KWS]')
        if self.isClassMethod() or self.isStaticMethod():
            self.addChunk('_filter = lambda x, **kwargs: unicode(x)')
        else:
            self.addChunk('_filter = self._CHEETAH__currentFilter')
        self.addChunk('')
        self.addChunk("#" * 40)
        self.addChunk('## START - generated method body')
        self.addChunk('')

    def _addAutoCleanupCode(self):
        self.addChunk('')
        self.addChunk("#" * 40)
        self.addChunk('## END - generated method body')
        self.addChunk('')

        if not self._isGenerator:
            self.addStop()
        self.addChunk('')

    def addStop(self, expr=None):
        if self.setting('autoAssignDummyTransactionToSelf'):
            no_content = 'NO_CONTENT'
        else:
            no_content = "''"

        self.addChunk('if _dummyTrans:')
        self.indent()
        self.addChunk('self.transaction = None')
        self.addChunk('return trans.response().getvalue()')
        self.dedent()
        self.addChunk('else:')
        self.indent()
        self.addChunk('return %s' % no_content)
        self.dedent()

    def addMethArg(self, name, defVal=None):
        self._argStringList.append((name, defVal))

    def methodSignature(self):
        argStringChunks = []
        for arg in self._argStringList:
            chunk = arg[0]
            if chunk == 'self' and self.isClassMethod():
                chunk = 'cls'
            if chunk == 'self' and self.isStaticMethod():
                # Skip the "self" method for @staticmethod decorators
                continue
            if arg[1] is not None:
                chunk += '=' + arg[1]
            argStringChunks.append(chunk)
        argString = (', ').join(argStringChunks)

        output = []
        if self._decorators:
            output.append(''.join([self._indent + decorator + '\n'
                                   for decorator in self._decorators]))
        output.append(self._indent + "def "
                      + self.methodName() + "(" +
                      argString + "):\n\n")
        return ''.join(output)


##################################################
# CLASS COMPILERS


class ClassCompiler(GenUtils):
    methodCompilerClass = AutoMethodCompiler
    methodCompilerClassForInit = MethodCompiler

    def __init__(self, className, mainMethodName='respond',
                 moduleCompiler=None,
                 fileName=None,
                 settingsManager=None):

        self._settingsManager = settingsManager
        self._fileName = fileName
        self._className = className
        self._moduleCompiler = moduleCompiler
        self._mainMethodName = mainMethodName
        self._setupState()
        methodCompiler = self._spawnMethodCompiler(
            mainMethodName,
            initialMethodComment='## CHEETAH: main method generated for this template')

        self._setActiveMethodCompiler(methodCompiler)

    def setting(self, key):
        return self._settingsManager.setting(key)

    def __getattr__(self, name):
        """Provide access to the methods and attributes of the MethodCompiler
        at the top of the activeMethods stack: one-way namespace sharing


        WARNING: Use .setMethods to assign the attributes of the MethodCompiler
        from the methods of this class!!! or you will be assigning to attributes
        of this object instead."""

        if self._activeMethodsList and hasattr(self._activeMethodsList[-1], name):
            return getattr(self._activeMethodsList[-1], name)
        else:
            raise AttributeError(name)

    def _setupState(self):
        self._decoratorsForNextMethod = []
        self._activeMethodsList = []        # stack while parsing/generating
        self._finishedMethodsList = []      # store by order
        self._methodsIndex = {}      # store by name
        self._baseClass = 'Template'
        # printed after methods in the gen class def:
        self._generatedAttribs = []

        self._generatedAttribs.append('_CHEETAH_src = __CHEETAH_src__')

        self._blockMetaData = {}

    def cleanupState(self):
        while self._activeMethodsList:
            methCompiler = self._popActiveMethodCompiler()
            self._swallowMethodCompiler(methCompiler)
        self._setupInitMethod()

    def _setupInitMethod(self):
        __init__ = self._spawnMethodCompiler(
            '__init__',
            klass=self.methodCompilerClassForInit,
        )
        __init__.setMethodSignature("def __init__(self, *args, **KWs)")
        __init__.addChunk('super(%s, self).__init__(*args, **KWs)' % self._className)
        __init__.cleanupState()
        self._swallowMethodCompiler(__init__, pos=0)

    def className(self):
        return self._className

    def setBaseClass(self, baseClassName):
        self._baseClass = baseClassName

    def setMainMethodName(self, methodName):
        if methodName == self._mainMethodName:
            return
        # change the name in the methodCompiler and add new reference
        mainMethod = self._methodsIndex[self._mainMethodName]
        mainMethod.setMethodName(methodName)
        self._methodsIndex[methodName] = mainMethod

        # make sure that fileUpdate code still works properly:
        chunkToChange = ('write(self.' + self._mainMethodName + '(trans=trans))')
        chunks = mainMethod._methodBodyChunks
        if chunkToChange in chunks:
            for i in range(len(chunks)):
                if chunks[i] == chunkToChange:
                    chunks[i] = ('write(self.' + methodName + '(trans=trans))')
        # get rid of the old reference and update self._mainMethodName
        del self._methodsIndex[self._mainMethodName]
        self._mainMethodName = methodName

    def _spawnMethodCompiler(self, methodName, klass=None,
                             initialMethodComment=None):
        if klass is None:
            klass = self.methodCompilerClass

        decorators = self._decoratorsForNextMethod or []
        self._decoratorsForNextMethod = []
        methodCompiler = klass(methodName, classCompiler=self,
                               decorators=decorators,
                               initialMethodComment=initialMethodComment)
        self._methodsIndex[methodName] = methodCompiler
        return methodCompiler

    def _setActiveMethodCompiler(self, methodCompiler):
        self._activeMethodsList.append(methodCompiler)

    def _getActiveMethodCompiler(self):
        return self._activeMethodsList[-1]

    def _popActiveMethodCompiler(self):
        return self._activeMethodsList.pop()

    def _swallowMethodCompiler(self, methodCompiler, pos=None):
        methodCompiler.cleanupState()
        if pos is None:
            self._finishedMethodsList.append(methodCompiler)
        else:
            self._finishedMethodsList.insert(pos, methodCompiler)
        return methodCompiler

    def startMethodDef(self, methodName, argsList, parserComment):
        methodCompiler = self._spawnMethodCompiler(
            methodName, initialMethodComment=parserComment)
        self._setActiveMethodCompiler(methodCompiler)
        for argName, defVal in argsList:
            methodCompiler.addMethArg(argName, defVal)

    def _finishedMethods(self):
        return self._finishedMethodsList

    def addDecorator(self, decoratorExpr):
        """Set the decorator to be used with the next method in the source.

        See _spawnMethodCompiler() and MethodCompiler for the details of how
        this is used.
        """
        self._decoratorsForNextMethod.append(decoratorExpr)

    def addAttribute(self, attribExpr):
        # first test to make sure that the user hasn't used any fancy Cheetah syntax
        # (placeholders, directives, etc.) inside the expression
        if attribExpr.find('VFN(') != -1 or attribExpr.find('VFFSL(') != -1:
            raise ParseError(self,
                             'Invalid #attr directive.' +
                             ' It should only contain simple Python literals.')
        # now add the attribute
        self._generatedAttribs.append(attribExpr)

    def addSuper(self, argsList, parserComment=None):
        className = self._className
        methodName = self._getActiveMethodCompiler().methodName()

        argStringChunks = []
        for arg in argsList:
            chunk = arg[0]
            if arg[1] is not None:
                chunk += '=' + arg[1]
            argStringChunks.append(chunk)
        argString = ','.join(argStringChunks)

        self.addFilteredChunk(
            'super(%(className)s, self).%(methodName)s(%(argString)s)' % locals())

    def closeDef(self):
        self.commitStrConst()
        methCompiler = self._popActiveMethodCompiler()
        self._swallowMethodCompiler(methCompiler)

    def closeBlock(self):
        self.commitStrConst()
        methCompiler = self._popActiveMethodCompiler()
        methodName = methCompiler.methodName()
        self._swallowMethodCompiler(methCompiler)

        # metaData = self._blockMetaData[methodName]
        # rawDirective = metaData['raw']
        # lineCol = metaData['lineCol']

        # insert the code to call the block
        codeChunk = 'self.' + methodName + '(trans=trans)'
        self.addChunk(codeChunk)

        # self.appendToPrevChunk(' # generated from ' + repr(rawDirective) )
        # self.appendToPrevChunk(' at line %s, col %s' % lineCol + '.')

    # code wrapping methods

    def classDef(self):
        ind = self.setting('indentationStep')
        classDefChunks = [self.classSignature()]

        def addMethods():
            classDefChunks.extend([
                ind + '#'*50,
                ind + '## CHEETAH GENERATED METHODS',
                '\n',
                self.methodDefs(),
                ])

        def addAttributes():
            classDefChunks.extend([
                ind + '#'*50,
                ind + '## CHEETAH GENERATED ATTRIBUTES',
                '\n',
                self.attributes(),
                ])
        addMethods()
        addAttributes()

        classDef = '\n'.join(classDefChunks)
        return classDef

    def classSignature(self):
        return "class %s(%s):" % (self.className(), self._baseClass)

    def methodDefs(self):
        methodDefs = [methGen.methodDef() for methGen in self._finishedMethods()]
        return '\n\n'.join(methodDefs)

    def attributes(self):
        attribs = [
            self.setting('indentationStep') + five.text(attrib)
            for attrib in self._generatedAttribs
        ]
        return '\n\n'.join(attribs)


##################################################
# MODULE COMPILERS


class Compiler(SettingsManager, GenUtils):
    parserClass = Parser
    classCompilerClass = ClassCompiler

    def __init__(self,
                 source,
                 moduleName='DynamicallyCompiledCheetahTemplate',
                 mainClassName=None,  # string
                 mainMethodName=None,  # string
                 baseclassName=None,  # string
                 settings=None  # dict
                 ):
        super(Compiler, self).__init__()
        if settings:
            self.updateSettings(settings)

        self._compiled = False
        self._moduleName = moduleName
        if not mainClassName:
            self._mainClassName = moduleName
        else:
            self._mainClassName = mainClassName
        self._mainMethodNameArg = mainMethodName
        if mainMethodName:
            self.setSetting('mainMethodName', mainMethodName)
        self._baseclassName = baseclassName

        self._filePath = None

        if self._filePath:
            self._fileDirName, self._fileBaseName = os.path.split(self._filePath)
            self._fileBaseNameRoot, self._fileBaseNameExt = os.path.splitext(self._fileBaseName)

        assert isinstance(source, five.text), 'the yelp-cheetah compiler requires text, not bytes.'

        if source == "":
            warnings.warn("You supplied an empty string for the source!", )

        self._parser = self.parserClass(source, filename=self._filePath, compiler=self)
        self._setupCompilerState()

    def __getattr__(self, name):
        """Provide one-way access to the methods and attributes of the
        ClassCompiler, and thereby the MethodCompilers as well.

        WARNING: Use .setMethods to assign the attributes of the ClassCompiler
        from the methods of this class!!! or you will be assigning to attributes
        of this object instead.
        """
        if self._activeClassesList and hasattr(self._activeClassesList[-1], name):
            return getattr(self._activeClassesList[-1], name)
        else:
            raise AttributeError(name)

    def _initializeSettings(self):
        self.updateSettings(copy.deepcopy(DEFAULT_COMPILER_SETTINGS))

    def _setupCompilerState(self):
        self._activeClassesList = []
        self._finishedClassesList = []      # listed by ordered
        self._finishedClassIndex = {}  # listed by name
        self._moduleDef = None
        self._moduleEncoding = 'ascii'
        self._moduleEncodingStr = ''
        self._moduleHeaderLines = []
        self._specialVars = {}
        self._importStatements = [
            "import sys",
            "import os",
            "import os.path",
            'try:',
            '    import builtins as builtin',
            'except ImportError:',
            '    import __builtin__ as builtin',
            "from os.path import getmtime, exists",
            "import types",
            "from Cheetah.Template import NO_CONTENT",
            "from Cheetah.Template import Template",
            "from Cheetah.DummyTransaction import DummyTransaction",
            "from Cheetah.NameMapper import NotFound, valueForName, valueFromSearchList, valueFromFrameOrSearchList",
            "import Cheetah.Filters as Filters",
        ]

        self._importedVarNames = ['sys',
                                  'os',
                                  'os.path',
                                  'types',
                                  'Template',
                                  'DummyTransaction',
                                  'NotFound',
                                  'Filters',
                                  ]

        self._moduleConstants = [
            "VFFSL=valueFromFrameOrSearchList",
            "VFSL=valueFromSearchList",
            "VFN=valueForName",
        ]

    def compile(self):
        classCompiler = self._spawnClassCompiler(self._mainClassName)
        if self._baseclassName:
            classCompiler.setBaseClass(self._baseclassName)
        self._addActiveClassCompiler(classCompiler)
        self._parser.parse()
        self._swallowClassCompiler(self._popActiveClassCompiler())
        self._compiled = True
        self._parser.cleanup()

    def _spawnClassCompiler(self, className, klass=None):
        if klass is None:
            klass = self.classCompilerClass
        classCompiler = klass(className,
                              moduleCompiler=self,
                              mainMethodName=self.setting('mainMethodName'),
                              fileName=self._filePath,
                              settingsManager=self,
                              )
        return classCompiler

    def _addActiveClassCompiler(self, classCompiler):
        self._activeClassesList.append(classCompiler)

    def _getActiveClassCompiler(self):
        return self._activeClassesList[-1]

    def _popActiveClassCompiler(self):
        return self._activeClassesList.pop()

    def _swallowClassCompiler(self, classCompiler):
        classCompiler.cleanupState()
        self._finishedClassesList.append(classCompiler)
        self._finishedClassIndex[classCompiler.className()] = classCompiler
        return classCompiler

    def _finishedClasses(self):
        return self._finishedClassesList

    def importedVarNames(self):
        return self._importedVarNames

    def addImportedVarNames(self, varNames, raw_statement=None):
        settings = self.settings()
        if not varNames:
            return
        if not settings.get('useLegacyImportMode'):
            if raw_statement and getattr(self, '_methodBodyChunks'):
                self.addChunk(raw_statement)
        else:
            self._importedVarNames.extend(varNames)

    # methods for adding stuff to the module and class definitions

    def setBaseClass(self, baseClassName):
        if self._mainMethodNameArg:
            self.setMainMethodName(self._mainMethodNameArg)
        else:
            self.setMainMethodName(self.setting('mainMethodNameForSubclasses'))

        if (
            not self.setting('autoImportForExtendsDirective') or
            baseClassName == 'object' or
            baseClassName in self.importedVarNames()
        ):
            self._getActiveClassCompiler().setBaseClass(baseClassName)
            # no need to import
        else:
            ##################################################
            # If the #extends directive contains a classname or modulename that isn't
            # in self.importedVarNames() already, we assume that we need to add
            # an implied 'from ModName import ClassName' where ModName == ClassName.
            # - This is the case in WebKit servlet modules.
            # - We also assume that the final . separates the classname from the
            #   module name.  This might break if people do something really fancy
            #   with their dots and namespaces.
            baseclasses = baseClassName.split(',')
            for klass in baseclasses:
                chunks = klass.split('.')
                if len(chunks) == 1:
                    self._getActiveClassCompiler().setBaseClass(klass)
                    if klass not in self.importedVarNames():
                        modName = klass
                        # we assume the class name to be the module name
                        # and that it's not a builtin:
                        importStatement = "from %s import %s" % (modName, klass)
                        self.addImportStatement(importStatement)
                        self.addImportedVarNames((klass,))
                else:
                    needToAddImport = True
                    modName = chunks[0]
                    # print chunks, ':', self.importedVarNames()
                    for chunk in chunks[1:-1]:
                        if modName in self.importedVarNames():
                            needToAddImport = False
                            finalBaseClassName = klass.replace(modName + '.', '')
                            self._getActiveClassCompiler().setBaseClass(finalBaseClassName)
                            break
                        else:
                            modName += '.' + chunk
                    if needToAddImport:
                        modName, finalClassName = '.'.join(chunks[:-1]), chunks[-1]
                        # if finalClassName != chunks[:-1][-1]:
                        if finalClassName != chunks[-2]:
                            # we assume the class name to be the module name
                            modName = '.'.join(chunks)
                        self._getActiveClassCompiler().setBaseClass(finalClassName)
                        importStatement = "from %s import %s" % (modName, finalClassName)
                        self.addImportStatement(importStatement)
                        self.addImportedVarNames([finalClassName])

    def setCompilerSetting(self, key, valueExpr):
        self.setSetting(key, eval(valueExpr))
        self._parser.configureParser()

    def setCompilerSettings(self, keywords, settingsStr):
        settingsReader = self.updateSettingsFromConfigStr

        settingsReader(settingsStr)
        self._parser.configureParser()

    def setModuleEncoding(self, encoding):
        self._moduleEncoding = encoding

    def getModuleEncoding(self):
        return self._moduleEncoding

    def addModuleHeader(self, line):
        """Adds a header comment to the top of the generated module.
        """
        self._moduleHeaderLines.append(line)

    def addModuleGlobal(self, line):
        """Adds a line of global module code.  It is inserted after the import
        statements and Cheetah default module constants.
        """
        self._moduleConstants.append(line)

    def addSpecialVar(self, basename, contents, includeUnderscores=True):
        """Adds module __specialConstant__ to the module globals.
        """
        name = includeUnderscores and '__' + basename + '__' or basename
        self._specialVars[name] = contents.strip()

    def addImportStatement(self, impStatement):
        settings = self.settings()
        if not self._methodBodyChunks or settings.get('useLegacyImportMode'):
            # In the case where we are importing inline in the middle of a source block
            # we don't want to inadvertantly import the module at the top of the file either
            self._importStatements.append(impStatement)

        # @@TR 2005-01-01: there's almost certainly a cleaner way to do this!
        importVarNames = impStatement[impStatement.find('import') + len('import'):].split(',')
        importVarNames = [var.split()[-1] for var in importVarNames]  # handles aliases
        importVarNames = [var for var in importVarNames if not var == '*']
        self.addImportedVarNames(importVarNames, raw_statement=impStatement)  # used by #extend for auto-imports

    def addAttribute(self, attribName, expr):
        self._getActiveClassCompiler().addAttribute(attribName + ' =' + expr)

    def addComment(self, comm):
        if re.match(r'#+$', comm):      # skip bar comments
            return

        for line in comm.splitlines():
            self.addMethComment(line)

    # methods for module code wrapping

    def getModuleCode(self):
        if not self._compiled:
            self.compile()
        if self._moduleDef:
            return self._moduleDef
        else:
            return self.wrapModuleDef()

    __str__ = getModuleCode

    def wrapModuleDef(self):
        if self._filePath:
            self.addModuleGlobal('__CHEETAH_src__ = %r' % self._filePath)
        else:
            self.addModuleGlobal('__CHEETAH_src__ = None')

        moduleDef = textwrap.dedent(
            """
            %(header)s

            %(imports)s

            %(constants)s
            %(specialVars)s

            %(classes)s

            %(footer)s
            """
        ).strip() % {
            'header': self.moduleHeader(),
            'specialVars': self.specialVars(),
            'imports': self.importStatements(),
            'constants': self.moduleConstants(),
            'classes': self.classDefs(),
            'footer': self.moduleFooter(),
            'mainClassName': self._mainClassName,
        }

        self._moduleDef = moduleDef
        return moduleDef

    def timestamp(self):
        return time.asctime(time.localtime(time.time()))

    def moduleHeader(self):
        header = self._moduleEncodingStr + '\n'
        if self._moduleHeaderLines:
            offSet = self.setting('commentOffset')

            header += (
                '#' + ' ' * offSet +
                ('\n#' + ' ' * offSet).join(self._moduleHeaderLines) + '\n')

        return header

    def specialVars(self):
        chunks = []
        theVars = self._specialVars
        keys = sorted(theVars.keys())
        for key in keys:
            chunks.append(key + ' = ' + repr(theVars[key]))
        return '\n'.join(chunks)

    def importStatements(self):
        return '\n'.join(self._importStatements)

    def moduleConstants(self):
        return '\n'.join(self._moduleConstants)

    def classDefs(self):
        classDefs = [klass.classDef() for klass in self._finishedClasses()]
        return '\n\n'.join(classDefs)

    def moduleFooter(self):
        return """
# CHEETAH was developed by Tavis Rudd and Mike Orr
# with code, advice and input from many other volunteers.
# For more information visit http://www.CheetahTemplate.org/

if __name__ == '__main__':
    from os import environ
    from sys import stdout
    stdout.write({main_class_name}(searchList=[environ]).respond())
""".format(main_class_name=self._mainClassName)

# vim: shiftwidth=4 tabstop=4 expandtab
