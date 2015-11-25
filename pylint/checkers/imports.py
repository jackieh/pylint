# Copyright (c) 2003-2013 LOGILAB S.A. (Paris, FRANCE).
# http://www.logilab.fr/ -- mailto:contact@logilab.fr
#
# This program is free software; you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation; either version 2 of the License, or (at your option) any later
# version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE. See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along with
# this program; if not, write to the Free Software Foundation, Inc.,
# 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301, USA.
"""imports checkers for Python code"""

import os
import sys
from collections import defaultdict, Counter

import six

import astroid
from astroid import are_exclusive
from astroid.modutils import (EXT_LIB_DIR, get_module_part, is_standard_module,
                              file_from_modpath)

from pylint.interfaces import IAstroidChecker
from pylint.utils import EmptyReport, get_global_option
from pylint.checkers import BaseChecker
from pylint.checkers.utils import check_messages, node_ignores_exception
from pylint.graph import get_cycles, DotBackend
from pylint.reporters.ureports.nodes import VerbatimText, Paragraph


def _get_import_name(importnode, modname):
    """Get a prepared module name from the given import node

    In the case of relative imports, this will return the
    absolute qualified module name, which might be useful
    for debugging. Otherwise, the initial module name
    is returned unchanged.
    """
    if isinstance(importnode, astroid.ImportFrom):
        if importnode.level:
            root = importnode.root()
            if isinstance(root, astroid.Module):
                modname = root.relative_to_absolute_name(
                    modname, level=importnode.level)
    return modname


def get_first_import(node, context, name, base, level):
    """return the node where [base.]<name> is imported or None if not found
    """
    fullname = '%s.%s' % (base, name) if base else name

    first = None
    found = False
    for first in context.body:
        if first is node:
            continue
        if first.scope() is node.scope() and first.fromlineno > node.fromlineno:
            continue
        if isinstance(first, astroid.Import):
            if any(fullname == iname[0] for iname in first.names):
                found = True
                break
        elif isinstance(first, astroid.ImportFrom):
            if level == first.level and any(
                    fullname == '%s.%s' % (first.modname, iname[0])
                    for iname in first.names):
                found = True
                break
    if found and not are_exclusive(first, node):
        return first

# utilities to represents import dependencies as tree and dot graph ###########

def make_tree_defs(mod_files_list):
    """get a list of 2-uple (module, list_of_files_which_import_this_module),
    it will return a dictionary to represent this as a tree
    """
    tree_defs = {}
    for mod, files in mod_files_list:
        node = (tree_defs, ())
        for prefix in mod.split('.'):
            node = node[0].setdefault(prefix, [{}, []])
        node[1] += files
    return tree_defs

def repr_tree_defs(data, indent_str=None):
    """return a string which represents imports as a tree"""
    lines = []
    nodes = data.items()
    for i, (mod, (sub, files)) in enumerate(sorted(nodes, key=lambda x: x[0])):
        if not files:
            files = ''
        else:
            files = '(%s)' % ','.join(files)
        if indent_str is None:
            lines.append('%s %s' % (mod, files))
            sub_indent_str = '  '
        else:
            lines.append(r'%s\-%s %s' % (indent_str, mod, files))
            if i == len(nodes)-1:
                sub_indent_str = '%s  ' % indent_str
            else:
                sub_indent_str = '%s| ' % indent_str
        if sub:
            lines.append(repr_tree_defs(sub, sub_indent_str))
    return '\n'.join(lines)


def dependencies_graph(filename, dep_info):
    """write dependencies as a dot (graphviz) file
    """
    done = {}
    printer = DotBackend(filename[:-4], rankdir='LR')
    printer.emit('URL="." node[shape="box"]')
    for modname, dependencies in sorted(six.iteritems(dep_info)):
        done[modname] = 1
        printer.emit_node(modname)
        for modname in dependencies:
            if modname not in done:
                done[modname] = 1
                printer.emit_node(modname)
    for depmodname, dependencies in sorted(six.iteritems(dep_info)):
        for modname in dependencies:
            printer.emit_edge(modname, depmodname)
    printer.generate(filename)


def make_graph(filename, dep_info, sect, gtype):
    """generate a dependencies graph and add some information about it in the
    report's section
    """
    dependencies_graph(filename, dep_info)
    sect.append(Paragraph('%simports graph has been written to %s'
                          % (gtype, filename)))


# the import checker itself ###################################################

MSGS = {
    'E0401': ('Unable to import %s',
              'import-error',
              'Used when pylint has been unable to import a module.',
              {'old_names': [('F0401', 'import-error')]}),
    'R0401': ('Cyclic import (%s)',
              'cyclic-import',
              'Used when a cyclic import between two or more modules is \
              detected.'),

    'W0401': ('Wildcard import %s',
              'wildcard-import',
              'Used when `from module import *` is detected.'),
    'W0402': ('Uses of a deprecated module %r',
              'deprecated-module',
              'Used a module marked as deprecated is imported.'),
    'W0403': ('Relative import %r, should be %r',
              'relative-import',
              'Used when an import relative to the package directory is '
              'detected.',
              {'maxversion': (3, 0)}),
    'W0404': ('Reimport %r (imported line %s)',
              'reimported',
              'Used when a module is reimported multiple times.'),
    'W0406': ('Module import itself',
              'import-self',
              'Used when a module is importing itself.'),

    'W0410': ('__future__ import is not the first non docstring statement',
              'misplaced-future',
              'Python 2.5 and greater require __future__ import to be the \
              first non docstring statement in the module.'),

    'C0410': ('Multiple imports on one line (%s)',
              'multiple-imports',
              'Used when import statement importing multiple modules is '
              'detected.'),
    'C0411': ('%s comes before %s',
              'wrong-import-order',
              'Used when PEP8 import order is not respected (standard imports '
              'first, then third-party libraries, then local imports)'),
    'C0412': ('Imports from package %s are not grouped',
              'ungrouped-imports',
              'Used when imports are not grouped by packages'),
    'C0413': ('Import "%s" should be placed at the top of the '
              'module',
              'wrong-import-position',
              'Used when code and imports are mixed'),
    }

class ImportsChecker(BaseChecker):
    """checks for
    * external modules dependencies
    * relative / wildcard imports
    * cyclic imports
    * uses of deprecated modules
    """

    __implements__ = IAstroidChecker

    name = 'imports'
    msgs = MSGS
    priority = -2

    if sys.version_info < (3,):
        deprecated_modules = ('regsub', 'TERMIOS', 'Bastion', 'rexec')
    else:
        deprecated_modules = ('optparse', )
    options = (('deprecated-modules',
                {'default' : deprecated_modules,
                 'type' : 'csv',
                 'metavar' : '<modules>',
                 'help' : 'Deprecated modules which should not be used, \
separated by a comma'}
               ),
               ('import-graph',
                {'default' : '',
                 'type' : 'string',
                 'metavar' : '<file.dot>',
                 'help' : 'Create a graph of every (i.e. internal and \
external) dependencies in the given file (report RP0402 must not be disabled)'}
               ),
               ('ext-import-graph',
                {'default' : '',
                 'type' : 'string',
                 'metavar' : '<file.dot>',
                 'help' : 'Create a graph of external dependencies in the \
given file (report RP0402 must not be disabled)'}
               ),
               ('int-import-graph',
                {'default' : '',
                 'type' : 'string',
                 'metavar' : '<file.dot>',
                 'help' : 'Create a graph of internal dependencies in the \
given file (report RP0402 must not be disabled)'}
               ),
              )
    ext_lib_dir = os.path.normcase(os.path.abspath(EXT_LIB_DIR))

    def __init__(self, linter=None):
        BaseChecker.__init__(self, linter)
        self.stats = None
        self.import_graph = None
        self._imports_stack = []
        self._first_non_import_node = None
        self.__int_dep_info = self.__ext_dep_info = None
        self.reports = (('RP0401', 'External dependencies',
                         self.report_external_dependencies),
                        ('RP0402', 'Modules dependencies graph',
                         self.report_dependencies_graph),
                       )

    def open(self):
        """called before visiting project (i.e set of modules)"""
        self.linter.add_stats(dependencies={})
        self.linter.add_stats(cycles=[])
        self.stats = self.linter.stats
        self.import_graph = defaultdict(set)
        self._ignored_modules = get_global_option(
            self, 'ignored-modules', default=[])

    def close(self):
        """called before visiting project (i.e set of modules)"""
        # don't try to compute cycles if the associated message is disabled
        if self.linter.is_message_enabled('cyclic-import'):
            vertices = list(self.import_graph)
            for cycle in get_cycles(self.import_graph, vertices=vertices):
                self.add_message('cyclic-import', args=' -> '.join(cycle))

    @check_messages('wrong-import-position')
    def visit_import(self, node):
        """triggered when an import statement is seen"""
        modnode = node.root()
        names = [name for name, _ in node.names]
        if len(names) >= 2:
            self.add_message('multiple-imports', args=', '.join(names), node=node)
        for name in names:
            self._check_deprecated_module(node, name)
            importedmodnode = self.get_imported_module(node, name)
            if isinstance(node.scope(), astroid.Module):
                self._check_position(node)
                self._record_import(node, importedmodnode)
            if importedmodnode is None:
                continue
            self._check_relative_import(modnode, node, importedmodnode, name)
            self._add_imported_module(node, importedmodnode.name)
            self._check_reimport(node, name)

    # TODO This appears to be the list of all messages of the checker...
    # @check_messages('W0410', 'W0401', 'W0403', 'W0402', 'W0404', 'W0406', 'E0401')
    @check_messages(*(MSGS.keys()))
    def visit_importfrom(self, node):
        """triggered when a from statement is seen"""
        basename = node.modname
        if basename == '__future__':
            # check if this is the first non-docstring statement in the module
            prev = node.previous_sibling()
            if prev:
                # consecutive future statements are possible
                if not (isinstance(prev, astroid.ImportFrom)
                        and prev.modname == '__future__'):
                    self.add_message('misplaced-future', node=node)
            return
        self._check_deprecated_module(node, basename)
        for name, _ in node.names:
            if name == '*':
                self.add_message('wildcard-import', args=basename, node=node)
        modnode = node.root()
        importedmodnode = self.get_imported_module(node, basename)
        if isinstance(node.scope(), astroid.Module):
            self._check_position(node)
            self._record_import(node, importedmodnode)
        if importedmodnode is None:
            return
        self._check_relative_import(modnode, node, importedmodnode, basename)

        for name, _ in node.names:
            if name != '*':
                self._add_imported_module(node, '%s.%s' % (importedmodnode.name, name))
                self._check_reimport(node, name, basename, node.level)

        # Detect duplicate imports on the same line.
        names = (name for name, _ in node.names)
        counter = Counter(names)
        for name, count in counter.items():
            if count > 1:
                self.add_message('reimported', node=node,
                                 args=(name, node.fromlineno))

    @check_messages('wrong-import-order', 'ungrouped-imports')
    def leave_module(self, node):
        # Check imports are grouped by category (standard, 3rd party, local)
        std_imports, ext_imports, loc_imports = self._check_imports_order(node)

        # Check imports are grouped by package within a given category
        met = set()
        curr_package = None
        for imp in std_imports + ext_imports + loc_imports:
            package, _, _ = imp[1].partition('.')
            if curr_package and curr_package != package and package in met:
                self.add_message('ungrouped-imports', node=imp[0], args=package)
            curr_package = package
            met.add(package)

        self._imports_stack = []
        self._first_non_import_node = None

    def visit_if(self, node):
        # if the node does not contain an import instruction, and if it is the
        # first node of the module, keep a track of it (all the import positions
        # of the module will be compared to the position of this first
        # instruction)
        if self._first_non_import_node:
            return
        if not isinstance(node.parent, astroid.Module):
            return
        for _ in node.nodes_of_class((astroid.Import, astroid.ImportFrom)):
            return
        self._first_non_import_node = node

    visit_tryfinally = visit_tryexcept = visit_assignattr = visit_assign \
            = visit_ifexp = visit_comprehension = visit_if

    def visit_functiondef(self, node):
        # if it is the first non import instruction of the module, record it
        if not self._first_non_import_node:
            self._first_non_import_node = node

    visit_classdef = visit_for = visit_while = visit_functiondef

    def _check_position(self, node):
        """Check `node` import or importfrom node position is correct

        Send a message  if `node` comes before another instruction
        """
        # if a first non-import instruction has already been encountered,
        # it means the import comes after it and therefore is not well placed
        if self._first_non_import_node:
            self.add_message('wrong-import-position', node=node,
                             args=node.as_string())

    def _record_import(self, node, importedmodnode):
        """Record the package `node` imports from"""
        importedname = importedmodnode.name if importedmodnode else None
        if not importedname:
            importedname = node.names[0][0].split('.')[0]
        self._imports_stack.append((node, importedname))

    def _check_imports_order(self, node):
        """Checks imports of module `node` are grouped by category

        Imports must follow this order: standard, 3rd party, local
        """
        extern_imports = []
        local_imports = []
        std_imports = []
        stdlib_paths = [sys.prefix, self.ext_lib_dir]
        real_prefix = getattr(sys, 'real_prefix', None)
        if real_prefix is not None:
            stdlib_paths.append(real_prefix)

        for node, modname in self._imports_stack:
            package = modname.split('.')[0]
            if is_standard_module(modname):
                std_imports.append((node, package))
                wrong_import = extern_imports or local_imports
                if not wrong_import:
                    continue
                self.add_message('wrong-import-order', node=node,
                                 args=('standard import "%s"' % node.as_string(),
                                       '"%s"' % wrong_import[0][0].as_string()))
            else:
                try:
                    filename = file_from_modpath([package])
                except ImportError:
                    local_imports.append((node, package))
                    continue
                if not filename:
                    local_imports.append((node, package))
                    continue
                filename = os.path.normcase(os.path.abspath(filename))
                if not any(filename.startswith(path) for path in stdlib_paths):
                    local_imports.append((node, package))
                    continue
                extern_imports.append((node, package))
                if not local_imports:
                    continue
                self.add_message('wrong-import-order', node=node,
                                 args=('external import "%s"' % node.as_string(),
                                       '"%s"' % local_imports[0][0].as_string()))
        return std_imports, extern_imports, local_imports

    def get_imported_module(self, importnode, modname):
        try:
            return importnode.do_import_module(modname)
        except astroid.InferenceError as ex:
            dotted_modname = _get_import_name(importnode, modname)
            if str(ex) != modname:
                args = '%r (%s)' % (dotted_modname, ex)
            else:
                args = repr(dotted_modname)

            for submodule in self._qualified_names(modname):
                if submodule in self._ignored_modules:
                    return None

            if not node_ignores_exception(importnode, ImportError):
                self.add_message("import-error", args=args, node=importnode)

    @staticmethod
    def _qualified_names(modname):
        """Split the names of the given module into subparts

        For example,
            _qualified_names('pylint.checkers.ImportsChecker')
        returns
            ['pylint', 'pylint.checkers', 'pylint.checkers.ImportsChecker']
        """
        names = modname.split('.')
        return ['.'.join(names[0:i+1]) for i in range(len(names))]

    def _check_relative_import(self, modnode, importnode, importedmodnode,
                               importedasname):
        """check relative import. node is either an Import or From node, modname
        the imported module name.
        """
        if not self.linter.is_message_enabled('relative-import'):
            return
        if importedmodnode.file is None:
            return False # built-in module
        if modnode is importedmodnode:
            return False # module importing itself
        if modnode.absolute_import_activated() or getattr(importnode, 'level', None):
            return False
        if importedmodnode.name != importedasname:
            # this must be a relative import...
            self.add_message('relative-import',
                             args=(importedasname, importedmodnode.name),
                             node=importnode)

    def _add_imported_module(self, node, importedmodname):
        """notify an imported module, used to analyze dependencies"""
        try:
            importedmodname = get_module_part(importedmodname,
                                              node.root().file)
        except ImportError:
            pass
        context_name = node.root().name
        if context_name == importedmodname:
            # module importing itself !
            self.add_message('import-self', node=node)
        elif not is_standard_module(importedmodname):
            # handle dependencies
            importedmodnames = self.stats['dependencies'].setdefault(
                importedmodname, set())
            if context_name not in importedmodnames:
                importedmodnames.add(context_name)
            # update import graph
            mgraph = self.import_graph[context_name]
            if importedmodname not in mgraph:
                mgraph.add(importedmodname)

    def _check_deprecated_module(self, node, mod_path):
        """check if the module is deprecated"""
        for mod_name in self.config.deprecated_modules:
            if mod_path == mod_name or mod_path.startswith(mod_name + '.'):
                self.add_message('deprecated-module', node=node, args=mod_path)

    def _check_reimport(self, node, name, basename=None, level=None):
        """check if the import is necessary (i.e. not already done)"""
        if not self.linter.is_message_enabled('reimported'):
            return

        frame = node.frame()
        root = node.root()
        contexts = [(frame, level)]
        if root is not frame:
            contexts.append((root, None))
        for context, level in contexts:
            first = get_first_import(node, context, name, basename, level)
            if first is not None:
                self.add_message('reimported', node=node,
                                 args=(name, first.fromlineno))


    def report_external_dependencies(self, sect, _, dummy):
        """return a verbatim layout for displaying dependencies"""
        dep_info = make_tree_defs(six.iteritems(self._external_dependencies_info()))
        if not dep_info:
            raise EmptyReport()
        tree_str = repr_tree_defs(dep_info)
        sect.append(VerbatimText(tree_str))

    def report_dependencies_graph(self, sect, _, dummy):
        """write dependencies as a dot (graphviz) file"""
        dep_info = self.stats['dependencies']
        if not dep_info or not (self.config.import_graph
                                or self.config.ext_import_graph
                                or self.config.int_import_graph):
            raise EmptyReport()
        filename = self.config.import_graph
        if filename:
            make_graph(filename, dep_info, sect, '')
        filename = self.config.ext_import_graph
        if filename:
            make_graph(filename, self._external_dependencies_info(),
                       sect, 'external ')
        filename = self.config.int_import_graph
        if filename:
            make_graph(filename, self._internal_dependencies_info(),
                       sect, 'internal ')

    def _external_dependencies_info(self):
        """return cached external dependencies information or build and
        cache them
        """
        if self.__ext_dep_info is None:
            package = self.linter.current_name
            self.__ext_dep_info = result = {}
            for importee, importers in six.iteritems(self.stats['dependencies']):
                if not importee.startswith(package):
                    result[importee] = importers
        return self.__ext_dep_info

    def _internal_dependencies_info(self):
        """return cached internal dependencies information or build and
        cache them
        """
        if self.__int_dep_info is None:
            package = self.linter.current_name
            self.__int_dep_info = result = {}
            for importee, importers in six.iteritems(self.stats['dependencies']):
                if importee.startswith(package):
                    result[importee] = importers
        return self.__int_dep_info


def register(linter):
    """required method to auto register this checker """
    linter.register_checker(ImportsChecker(linter))
