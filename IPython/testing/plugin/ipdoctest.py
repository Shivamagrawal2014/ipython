"""Nose Plugin that supports IPython doctests.

Limitations:

- When generating examples for use as doctests, make sure that you have
  pretty-printing OFF.  This can be done either by starting ipython with the
  flag '--nopprint', by setting pprint to 0 in your ipythonrc file, or by
  interactively disabling it with %Pprint.  This is required so that IPython
  output matches that of normal Python, which is used by doctest for internal
  execution.

- Do not rely on specific prompt numbers for results (such as using
  '_34==True', for example).  For IPython tests run via an external process the
  prompt numbers may be different, and IPython tests run as normal python code
  won't even have these special _NN variables set at all.

- IPython functions that produce output as a side-effect of calling a system
  process (e.g. 'ls') can be doc-tested, but they must be handled in an
  external IPython process.  Such doctests must be tagged with:

        # ipdoctest: EXTERNAL

  so that the testing machinery handles them differently.  Since these are run
  via pexpect in an external process, they can't deal with exceptions or other
  fancy featurs of regular doctests.  You must limit such tests to simple
  matching of the output.  For this reason, I recommend you limit these kinds
  of doctests to features that truly require a separate process, and use the
  normal IPython ones (which have all the features of normal doctests) for
  everything else.  See the examples at the bottom of this file for a
  comparison of what can be done with both types.
"""


#-----------------------------------------------------------------------------
# Module imports

# From the standard library
import __builtin__
import commands
import doctest
import inspect
import logging
import os
import re
import sys
import traceback
import unittest

from inspect import getmodule
from StringIO import StringIO

# We are overriding the default doctest runner, so we need to import a few
# things from doctest directly
from doctest import (REPORTING_FLAGS, REPORT_ONLY_FIRST_FAILURE,
                     _unittest_reportflags, DocTestRunner,
                     _extract_future_flags, pdb, _OutputRedirectingPdb,
                     _exception_traceback,
                     linecache)

# Third-party modules
import nose.core

from nose.plugins import doctests, Plugin
from nose.util import anyp, getpackage, test_address, resolve_name, tolist

# Our own imports
#from extdoctest import ExtensionDoctest, DocTestFinder
#from dttools import DocTestFinder, DocTestCase
#-----------------------------------------------------------------------------
# Module globals and other constants

log = logging.getLogger(__name__)

###########################################################################
# *** HACK ***
# We must start our own ipython object and heavily muck with it so that all the
# modifications IPython makes to system behavior don't send the doctest
# machinery into a fit.  This code should be considered a gross hack, but it
# gets the job done.


# XXX - Hack to modify the %run command so we can sync the user's namespace
# with the test globals.  Once we move over to a clean magic system, this will
# be done with much less ugliness.

def _run_ns_sync(self,arg_s,runner=None):
    """Modified version of %run that syncs testing namespaces.

    This is strictly needed for running doctests that call %run.
    """

    out = _ip.IP.magic_run_ori(arg_s,runner)
    _run_ns_sync.test_globs.update(_ip.user_ns)
    return out


# XXX1 - namespace handling
class ncdict(dict):
    def __init__(self,*a):
        dict.__init__(self,*a)
        self._savedict = {}
        
    def copy(self):
        return self

    def clear(self):
        import IPython
        
        print 'NCDICT - clear'   # dbg
        dict.clear(self)
        self.update(IPython.ipapi.make_user_ns())
        self.update(self._savedict)

    def remember(self,adict):
        self._savedict = adict
    
#class ncdict(dict): pass

def start_ipython():
    """Start a global IPython shell, which we need for IPython-specific syntax.
    """
    import new

    import IPython

    def xsys(cmd):
        """Execute a command and print its output.

        This is just a convenience function to replace the IPython system call
        with one that is more doctest-friendly.
        """
        cmd = _ip.IP.var_expand(cmd,depth=1)
        sys.stdout.write(commands.getoutput(cmd))
        sys.stdout.flush()

    # Store certain global objects that IPython modifies
    _displayhook = sys.displayhook
    _excepthook = sys.excepthook
    _main = sys.modules.get('__main__')

    # Start IPython instance.  We customize it to start with minimal frills.
    user_ns,global_ns = IPython.ipapi.make_user_namespaces(ncdict(),dict())
    
    IPython.Shell.IPShell(['--classic','--noterm_title'],
                          user_ns,global_ns)

    # Deactivate the various python system hooks added by ipython for
    # interactive convenience so we don't confuse the doctest system
    sys.modules['__main__'] = _main
    sys.displayhook = _displayhook
    sys.excepthook = _excepthook

    # So that ipython magics and aliases can be doctested (they work by making
    # a call into a global _ip object)
    _ip = IPython.ipapi.get()
    __builtin__._ip = _ip

    # Modify the IPython system call with one that uses getoutput, so that we
    # can capture subcommands and print them to Python's stdout, otherwise the
    # doctest machinery would miss them.
    _ip.system = xsys

    # Also patch our %run function in.
    im = new.instancemethod(_run_ns_sync,_ip.IP, _ip.IP.__class__)
    _ip.IP.magic_run_ori = _ip.IP.magic_run
    _ip.IP.magic_run = im

# The start call MUST be made here.  I'm not sure yet why it doesn't work if
# it is made later, at plugin initialization time, but in all my tests, that's
# the case.
start_ipython()

# *** END HACK ***
###########################################################################

# Classes and functions

def is_extension_module(filename):
    """Return whether the given filename is an extension module.

    This simply checks that the extension is either .so or .pyd.
    """
    return os.path.splitext(filename)[1].lower() in ('.so','.pyd')


class nodoc(object):
    def __init__(self,obj):
        self.obj = obj

    def __getattribute__(self,key):
        if key == '__doc__':
            return None
        else:
            return getattr(object.__getattribute__(self,'obj'),key)

# Modified version of the one in the stdlib, that fixes a python bug (doctests
# not found in extension modules, http://bugs.python.org/issue3158)
class DocTestFinder(doctest.DocTestFinder):

    def _from_module(self, module, object):
        """
        Return true if the given object is defined in the given
        module.
        """
        if module is None:
            return True
        elif inspect.isfunction(object):
            return module.__dict__ is object.func_globals
        elif inspect.isbuiltin(object):
            return module.__name__ == object.__module__
        elif inspect.isclass(object):
            return module.__name__ == object.__module__
        elif inspect.ismethod(object):
            # This one may be a bug in cython that fails to correctly set the
            # __module__ attribute of methods, but since the same error is easy
            # to make by extension code writers, having this safety in place
            # isn't such a bad idea
            return module.__name__ == object.im_class.__module__
        elif inspect.getmodule(object) is not None:
            return module is inspect.getmodule(object)
        elif hasattr(object, '__module__'):
            return module.__name__ == object.__module__
        elif isinstance(object, property):
            return True # [XX] no way not be sure.
        else:
            raise ValueError("object must be a class or function")

    def _find(self, tests, obj, name, module, source_lines, globs, seen):
        """
        Find tests for the given object and any contained objects, and
        add them to `tests`.
        """

        if hasattr(obj,"skip_doctest"):
            #print 'SKIPPING DOCTEST FOR:',obj  # dbg
            obj = nodoc(obj)

        doctest.DocTestFinder._find(self,tests, obj, name, module,
                                    source_lines, globs, seen)

        # Below we re-run pieces of the above method with manual modifications,
        # because the original code is buggy and fails to correctly identify
        # doctests in extension modules.

        # Local shorthands
        from inspect import isroutine, isclass, ismodule

        # Look for tests in a module's contained objects.
        if inspect.ismodule(obj) and self._recurse:
            for valname, val in obj.__dict__.items():
                valname1 = '%s.%s' % (name, valname)
                if ( (isroutine(val) or isclass(val))
                     and self._from_module(module, val) ):

                    self._find(tests, val, valname1, module, source_lines,
                               globs, seen)

        # Look for tests in a class's contained objects.
        if inspect.isclass(obj) and self._recurse:
            #print 'RECURSE into class:',obj  # dbg
            for valname, val in obj.__dict__.items():
                # Special handling for staticmethod/classmethod.
                if isinstance(val, staticmethod):
                    val = getattr(obj, valname)
                if isinstance(val, classmethod):
                    val = getattr(obj, valname).im_func

                # Recurse to methods, properties, and nested classes.
                if ((inspect.isfunction(val) or inspect.isclass(val) or
                     inspect.ismethod(val) or
                      isinstance(val, property)) and
                      self._from_module(module, val)):
                    valname = '%s.%s' % (name, valname)
                    self._find(tests, val, valname, module, source_lines,
                               globs, seen)


    # XXX1 - namespace handling
    def Xfind(self, obj, name=None, module=None, globs=None, extraglobs=None):
        """
        Return a list of the DocTests that are defined by the given
        object's docstring, or by any of its contained objects'
        docstrings.

        The optional parameter `module` is the module that contains
        the given object.  If the module is not specified or is None, then
        the test finder will attempt to automatically determine the
        correct module.  The object's module is used:

            - As a default namespace, if `globs` is not specified.
            - To prevent the DocTestFinder from extracting DocTests
              from objects that are imported from other modules.
            - To find the name of the file containing the object.
            - To help find the line number of the object within its
              file.

        Contained objects whose module does not match `module` are ignored.

        If `module` is False, no attempt to find the module will be made.
        This is obscure, of use mostly in tests:  if `module` is False, or
        is None but cannot be found automatically, then all objects are
        considered to belong to the (non-existent) module, so all contained
        objects will (recursively) be searched for doctests.

        The globals for each DocTest is formed by combining `globs`
        and `extraglobs` (bindings in `extraglobs` override bindings
        in `globs`).  A new copy of the globals dictionary is created
        for each DocTest.  If `globs` is not specified, then it
        defaults to the module's `__dict__`, if specified, or {}
        otherwise.  If `extraglobs` is not specified, then it defaults
        to {}.

        """

        # Find the module that contains the given object (if obj is
        # a module, then module=obj.).  Note: this may fail, in which
        # case module will be None.
        if module is False:
            module = None
        elif module is None:
            module = inspect.getmodule(obj)

        # always build our own globals
        if globs is None:
            if module is None:
                globs = {}
            else:
                globs = module.__dict__.copy()
        else:
            globs.update(module.__dict__.copy())

        print 'globs is:',globs.keys()
        
        if extraglobs is not None:
            globs.update(extraglobs)

        try:
            globs.remember(module.__dict__)
        except:
            pass
        
        ## # Initialize globals, and merge in extraglobs.
        ## if globs is None:
        ##     if module is None:
        ##         globs = {}
        ##     else:
        ##         globs = module.__dict__.copy()
        ## else:
        ##     globs = globs.copy()
        ## if extraglobs is not None:
        ##     globs.update(extraglobs)

        return doctest.DocTestFinder.find(self,obj,name,module,globs,
                                          extraglobs)


class IPDoctestOutputChecker(doctest.OutputChecker):
    """Second-chance checker with support for random tests.
    
    If the default comparison doesn't pass, this checker looks in the expected
    output string for flags that tell us to ignore the output.
    """

    random_re = re.compile(r'#\s*random\s+')
    
    def check_output(self, want, got, optionflags):
        """Check output, accepting special markers embedded in the output.

        If the output didn't pass the default validation but the special string
        '#random' is included, we accept it."""

        # Let the original tester verify first, in case people have valid tests
        # that happen to have a comment saying '#random' embedded in.
        ret = doctest.OutputChecker.check_output(self, want, got,
                                                 optionflags)
        if not ret and self.random_re.search(want):
            #print >> sys.stderr, 'RANDOM OK:',want  # dbg
            return True

        return ret


class DocTestCase(doctests.DocTestCase):
    """Proxy for DocTestCase: provides an address() method that
    returns the correct address for the doctest case. Otherwise
    acts as a proxy to the test case. To provide hints for address(),
    an obj may also be passed -- this will be used as the test object
    for purposes of determining the test address, if it is provided.
    """

    # Note: this method was taken from numpy's nosetester module.

    # Subclass nose.plugins.doctests.DocTestCase to work around a bug in
    # its constructor that blocks non-default arguments from being passed
    # down into doctest.DocTestCase

    def __init__(self, test, optionflags=0, setUp=None, tearDown=None,
                 checker=None, obj=None, result_var='_'):
        self._result_var = result_var
        doctests.DocTestCase.__init__(self, test,
                                      optionflags=optionflags,
                                      setUp=setUp, tearDown=tearDown,
                                      checker=checker)
        # Now we must actually copy the original constructor from the stdlib
        # doctest class, because we can't call it directly and a bug in nose
        # means it never gets passed the right arguments.

        self._dt_optionflags = optionflags
        self._dt_checker = checker
        self._dt_test = test
        self._dt_setUp = setUp
        self._dt_tearDown = tearDown

        # Each doctest should remember what directory it was loaded from...
        self._ori_dir = os.getcwd()

    # Modified runTest from the default stdlib
    def runTest(self):
        test = self._dt_test
        old = sys.stdout
        new = StringIO()
        optionflags = self._dt_optionflags

        if not (optionflags & REPORTING_FLAGS):
            # The option flags don't include any reporting flags,
            # so add the default reporting flags
            optionflags |= _unittest_reportflags

        runner = IPDocTestRunner(optionflags=optionflags,
                                 checker=self._dt_checker, verbose=False)

        try:
            # Save our current directory and switch out to the one where the
            # test was originally created, in case another doctest did a
            # directory change.  We'll restore this in the finally clause.
            curdir = os.getcwd()
            os.chdir(self._ori_dir)

            runner.DIVIDER = "-"*70
            failures, tries = runner.run(
                test, out=new.write, clear_globs=False)
        finally:
            sys.stdout = old
            os.chdir(curdir)

        if failures:
            raise self.failureException(self.format_failure(new.getvalue()))

    # XXX1 - namespace handling
    def XtearDown(self):
        print '!! teardown!'  # dbg
        doctests.DocTestCase.tearDown(self)



# A simple subclassing of the original with a different class name, so we can
# distinguish and treat differently IPython examples from pure python ones.
class IPExample(doctest.Example): pass


class IPExternalExample(doctest.Example):
    """Doctest examples to be run in an external process."""

    def __init__(self, source, want, exc_msg=None, lineno=0, indent=0,
                 options=None):
        # Parent constructor
        doctest.Example.__init__(self,source,want,exc_msg,lineno,indent,options)

        # An EXTRA newline is needed to prevent pexpect hangs
        self.source += '\n'


class IPDocTestParser(doctest.DocTestParser):
    """
    A class used to parse strings containing doctest examples.

    Note: This is a version modified to properly recognize IPython input and
    convert any IPython examples into valid Python ones.
    """
    # This regular expression is used to find doctest examples in a
    # string.  It defines three groups: `source` is the source code
    # (including leading indentation and prompts); `indent` is the
    # indentation of the first (PS1) line of the source code; and
    # `want` is the expected output (including leading indentation).

    # Classic Python prompts or default IPython ones
    _PS1_PY = r'>>>'
    _PS2_PY = r'\.\.\.'

    _PS1_IP = r'In\ \[\d+\]:'
    _PS2_IP = r'\ \ \ \.\.\.+:'

    _RE_TPL = r'''
        # Source consists of a PS1 line followed by zero or more PS2 lines.
        (?P<source>
            (?:^(?P<indent> [ ]*) (?P<ps1> %s) .*)    # PS1 line
            (?:\n           [ ]*  (?P<ps2> %s) .*)*)  # PS2 lines
        \n? # a newline
        # Want consists of any non-blank lines that do not start with PS1.
        (?P<want> (?:(?![ ]*$)    # Not a blank line
                     (?![ ]*%s)   # Not a line starting with PS1
                     (?![ ]*%s)   # Not a line starting with PS2
                     .*$\n?       # But any other line
                  )*)
                  '''

    _EXAMPLE_RE_PY = re.compile( _RE_TPL % (_PS1_PY,_PS2_PY,_PS1_PY,_PS2_PY),
                                 re.MULTILINE | re.VERBOSE)

    _EXAMPLE_RE_IP = re.compile( _RE_TPL % (_PS1_IP,_PS2_IP,_PS1_IP,_PS2_IP),
                                 re.MULTILINE | re.VERBOSE)

    # Mark a test as being fully random.  In this case, we simply append the
    # random marker ('#random') to each individual example's output.  This way
    # we don't need to modify any other code.
    _RANDOM_TEST = re.compile(r'#\s*all-random\s+')

    # Mark tests to be executed in an external process - currently unsupported.
    _EXTERNAL_IP = re.compile(r'#\s*ipdoctest:\s*EXTERNAL')
    
    def ip2py(self,source):
        """Convert input IPython source into valid Python."""
        out = []
        newline = out.append
        for lnum,line in enumerate(source.splitlines()):
            newline(_ip.IP.prefilter(line,lnum>0))
        newline('')  # ensure a closing newline, needed by doctest
        #print "PYSRC:", '\n'.join(out)  # dbg
        return '\n'.join(out)

    def parse(self, string, name='<string>'):
        """
        Divide the given string into examples and intervening text,
        and return them as a list of alternating Examples and strings.
        Line numbers for the Examples are 0-based.  The optional
        argument `name` is a name identifying this string, and is only
        used for error messages.
        """

        #print 'Parse string:\n',string # dbg

        string = string.expandtabs()
        # If all lines begin with the same indentation, then strip it.
        min_indent = self._min_indent(string)
        if min_indent > 0:
            string = '\n'.join([l[min_indent:] for l in string.split('\n')])

        output = []
        charno, lineno = 0, 0

        if self._RANDOM_TEST.search(string):
            random_marker = '\n# random'
        else:
            random_marker = ''

        # Whether to convert the input from ipython to python syntax
        ip2py = False
        # Find all doctest examples in the string.  First, try them as Python
        # examples, then as IPython ones
        terms = list(self._EXAMPLE_RE_PY.finditer(string))
        if terms:
            # Normal Python example
            #print '-'*70  # dbg
            #print 'PyExample, Source:\n',string  # dbg
            #print '-'*70  # dbg
            Example = doctest.Example
        else:
            # It's an ipython example.  Note that IPExamples are run
            # in-process, so their syntax must be turned into valid python.
            # IPExternalExamples are run out-of-process (via pexpect) so they
            # don't need any filtering (a real ipython will be executing them).
            terms = list(self._EXAMPLE_RE_IP.finditer(string))
            if self._EXTERNAL_IP.search(string):
                #print '-'*70  # dbg
                #print 'IPExternalExample, Source:\n',string  # dbg
                #print '-'*70  # dbg
                Example = IPExternalExample
            else:
                #print '-'*70  # dbg
                #print 'IPExample, Source:\n',string  # dbg
                #print '-'*70  # dbg
                Example = IPExample
                ip2py = True

        for m in terms:
            # Add the pre-example text to `output`.
            output.append(string[charno:m.start()])
            # Update lineno (lines before this example)
            lineno += string.count('\n', charno, m.start())
            # Extract info from the regexp match.
            (source, options, want, exc_msg) = \
                     self._parse_example(m, name, lineno,ip2py)

            # Append the random-output marker (it defaults to empty in most
            # cases, it's only non-empty for 'all-random' tests):
            want += random_marker
                
            if Example is IPExternalExample:
                options[doctest.NORMALIZE_WHITESPACE] = True
                want += '\n'

            # Create an Example, and add it to the list.
            if not self._IS_BLANK_OR_COMMENT(source):
                output.append(Example(source, want, exc_msg,
                                      lineno=lineno,
                                      indent=min_indent+len(m.group('indent')),
                                      options=options))
            # Update lineno (lines inside this example)
            lineno += string.count('\n', m.start(), m.end())
            # Update charno.
            charno = m.end()
        # Add any remaining post-example text to `output`.
        output.append(string[charno:])
        return output

    def _parse_example(self, m, name, lineno,ip2py=False):
        """
        Given a regular expression match from `_EXAMPLE_RE` (`m`),
        return a pair `(source, want)`, where `source` is the matched
        example's source code (with prompts and indentation stripped);
        and `want` is the example's expected output (with indentation
        stripped).

        `name` is the string's name, and `lineno` is the line number
        where the example starts; both are used for error messages.

        Optional:
        `ip2py`: if true, filter the input via IPython to convert the syntax
        into valid python.
        """

        # Get the example's indentation level.
        indent = len(m.group('indent'))

        # Divide source into lines; check that they're properly
        # indented; and then strip their indentation & prompts.
        source_lines = m.group('source').split('\n')

        # We're using variable-length input prompts
        ps1 = m.group('ps1')
        ps2 = m.group('ps2')
        ps1_len = len(ps1)

        self._check_prompt_blank(source_lines, indent, name, lineno,ps1_len)
        if ps2:
            self._check_prefix(source_lines[1:], ' '*indent + ps2, name, lineno)

        source = '\n'.join([sl[indent+ps1_len+1:] for sl in source_lines])

        if ip2py:
            # Convert source input from IPython into valid Python syntax
            source = self.ip2py(source)

        # Divide want into lines; check that it's properly indented; and
        # then strip the indentation.  Spaces before the last newline should
        # be preserved, so plain rstrip() isn't good enough.
        want = m.group('want')
        want_lines = want.split('\n')
        if len(want_lines) > 1 and re.match(r' *$', want_lines[-1]):
            del want_lines[-1]  # forget final newline & spaces after it
        self._check_prefix(want_lines, ' '*indent, name,
                           lineno + len(source_lines))

        # Remove ipython output prompt that might be present in the first line
        want_lines[0] = re.sub(r'Out\[\d+\]: \s*?\n?','',want_lines[0])

        want = '\n'.join([wl[indent:] for wl in want_lines])

        # If `want` contains a traceback message, then extract it.
        m = self._EXCEPTION_RE.match(want)
        if m:
            exc_msg = m.group('msg')
        else:
            exc_msg = None

        # Extract options from the source.
        options = self._find_options(source, name, lineno)

        return source, options, want, exc_msg

    def _check_prompt_blank(self, lines, indent, name, lineno, ps1_len):
        """
        Given the lines of a source string (including prompts and
        leading indentation), check to make sure that every prompt is
        followed by a space character.  If any line is not followed by
        a space character, then raise ValueError.

        Note: IPython-modified version which takes the input prompt length as a
        parameter, so that prompts of variable length can be dealt with.
        """
        space_idx = indent+ps1_len
        min_len = space_idx+1
        for i, line in enumerate(lines):
            if len(line) >=  min_len and line[space_idx] != ' ':
                raise ValueError('line %r of the docstring for %s '
                                 'lacks blank after %s: %r' %
                                 (lineno+i+1, name,
                                  line[indent:space_idx], line))


SKIP = doctest.register_optionflag('SKIP')


class IPDocTestRunner(doctest.DocTestRunner,object):
    """Test runner that synchronizes the IPython namespace with test globals.
    """
    
    def run(self, test, compileflags=None, out=None, clear_globs=True):

        # Hack: ipython needs access to the execution context of the example,
        # so that it can propagate user variables loaded by %run into
        # test.globs.  We put them here into our modified %run as a function
        # attribute.  Our new %run will then only make the namespace update
        # when called (rather than unconconditionally updating test.globs here
        # for all examples, most of which won't be calling %run anyway).
        _run_ns_sync.test_globs = test.globs

        # dbg
        ## print >> sys.stderr, "Test:",test
        ## for ex in test.examples:
        ##     print >> sys.stderr, ex.source
        ##     print >> sys.stderr, 'Want:\n',ex.want,'\n--'

        return super(IPDocTestRunner,self).run(test,
                                               compileflags,out,clear_globs)


class DocFileCase(doctest.DocFileCase):
    """Overrides to provide filename
    """
    def address(self):
        return (self._dt_test.filename, None, None)


class ExtensionDoctest(doctests.Doctest):
    """Nose Plugin that supports doctests in extension modules.
    """
    name = 'extdoctest'   # call nosetests with --with-extdoctest
    enabled = True

    def options(self, parser, env=os.environ):
        Plugin.options(self, parser, env)

    def configure(self, options, config):
        Plugin.configure(self, options, config)
        self.doctest_tests = options.doctest_tests
        self.extension = tolist(options.doctestExtension)
        self.finder = DocTestFinder()
        self.parser = doctest.DocTestParser()
        self.globs = None
        self.extraglobs = None

    def loadTestsFromExtensionModule(self,filename):
        bpath,mod = os.path.split(filename)
        modname = os.path.splitext(mod)[0]
        try:
            sys.path.append(bpath)
            module = __import__(modname)
            tests = list(self.loadTestsFromModule(module))
        finally:
            sys.path.pop()
        return tests

    # NOTE: the method below is almost a copy of the original one in nose, with
    # a  few modifications to control output checking.

    def loadTestsFromModule(self, module):
        #print 'lTM',module  # dbg

        if not self.matches(module.__name__):
            log.debug("Doctest doesn't want module %s", module)
            return

        tests = self.finder.find(module,globs=self.globs,
                                 extraglobs=self.extraglobs)
        if not tests:
            return

        tests.sort()
        module_file = module.__file__
        if module_file[-4:] in ('.pyc', '.pyo'):
            module_file = module_file[:-1]
        for test in tests:
            if not test.examples:
                continue
            if not test.filename:
                test.filename = module_file

            # xxx - checker and options may be ok instantiated once outside loop
            # always use whitespace and ellipsis options
            optionflags = doctest.NORMALIZE_WHITESPACE | doctest.ELLIPSIS
            checker = IPDoctestOutputChecker()
            
            yield DocTestCase(test,
                              optionflags=optionflags,
                              checker=checker)

    def loadTestsFromFile(self, filename):
        #print 'lTF',filename  # dbg

        if is_extension_module(filename):
            for t in self.loadTestsFromExtensionModule(filename):
                yield t
        else:
            if self.extension and anyp(filename.endswith, self.extension):
                name = os.path.basename(filename)
                dh = open(filename)
                try:
                    doc = dh.read()
                finally:
                    dh.close()
                test = self.parser.get_doctest(
                    doc, globs={'__file__': filename}, name=name,
                    filename=filename, lineno=0)
                if test.examples:
                    #print 'FileCase:',test.examples  # dbg
                    yield DocFileCase(test)
                else:
                    yield False # no tests to load

    def wantFile(self,filename):
        """Return whether the given filename should be scanned for tests.

        Modified version that accepts extension modules as valid containers for
        doctests.
        """
        #print 'Filename:',filename  # dbg

        # temporarily hardcoded list, will move to driver later
        exclude = ['IPython/external/',
                   'IPython/Extensions/ipy_',
                   'IPython/platutils_win32',
                   'IPython/frontend/cocoa',
                   'IPython_doctest_plugin',
                   'IPython/Gnuplot',
                   'IPython/Extensions/PhysicalQIn']

        for fex in exclude:
            if fex in filename:  # substring
                #print '###>>> SKIP:',filename  # dbg
                return False

        if is_extension_module(filename):
            return True
        else:
            return doctests.Doctest.wantFile(self,filename)


class IPythonDoctest(ExtensionDoctest):
    """Nose Plugin that supports doctests in extension modules.
    """
    name = 'ipdoctest'   # call nosetests with --with-ipdoctest
    enabled = True

    def configure(self, options, config):

        Plugin.configure(self, options, config)
        self.doctest_tests = options.doctest_tests
        self.extension = tolist(options.doctestExtension)
        self.parser = IPDocTestParser()
        self.finder = DocTestFinder(parser=self.parser)

        # XXX1 - namespace handling
        self.globs = None
        #self.globs = _ip.IP.user_ns
        
        self.extraglobs = None
