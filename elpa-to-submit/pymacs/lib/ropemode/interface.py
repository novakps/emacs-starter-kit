import os
import cStringIO

import rope.base.change
from rope.base import libutils
from rope.contrib import codeassist, generate, autoimport, findit

from ropemode import refactor, decorators, dialog


class RopeMode(object):

    def __init__(self, env):
        self.project = None
        self.old_content = None
        self.env = env

        self._prepare_refactorings()
        self.autoimport = None
        self._init_mode()

    def init(self):
        """Initialize rope mode"""

    def _init_mode(self):
        for attrname in dir(self):
            attr = getattr(self, attrname)
            if not callable(attr):
                continue
            kind = getattr(attr, 'kind', None)
            if kind == 'local':
                key = getattr(attr, 'local_key', None)
                prefix = getattr(attr, 'prefix', None)
                self.env.local_command(attrname, attr, key, prefix)
            if kind == 'global':
                key = getattr(attr, 'global_key', None)
                prefix = getattr(attr, 'prefix', None)
                self.env.global_command(attrname, attr, key, prefix)
            if kind == 'hook':
                hook = getattr(attr, 'hook', None)
                self.env.add_hook(attrname, attr, hook)

    def _prepare_refactorings(self):
        for name in dir(refactor):
            if not name.startswith('_') and name != 'Refactoring':
                attr = getattr(refactor, name)
                if isinstance(attr, type) and \
                   issubclass(attr, refactor.Refactoring):
                    refname = self._refactoring_name(attr)
                    @decorators.local_command(attr.key, 'P', None, refname)
                    def do_refactor(prefix, self=self, refactoring=attr):
                        initial_asking = prefix is None
                        refactoring(self, self.env).show(initial_asking=initial_asking)
                    setattr(self, refname, do_refactor)

    def _refactoring_name(self, refactoring):
        return refactor.refactoring_name(refactoring)

    @decorators.rope_hook('before_save')
    def before_save_actions(self):
        if self.project is not None:
            if not self._is_python_file(self.env.filename()):
                return
            resource = self._get_resource()
            if resource.exists():
                self.old_content = resource.read()
            else:
                self.old_content = ''

    @decorators.rope_hook('after_save')
    def after_save_actions(self):
        if self.project is not None and self.old_content is not None:
            libutils.report_change(self.project, self.env.filename(),
                                   self.old_content)
            self.old_content = None

    @decorators.rope_hook('exit')
    def exiting_actions(self):
        if self.project is not None:
            self.close_project()

    @decorators.global_command('o')
    def open_project(self, root=None):
        if not root:
            root = self.env.ask_directory('Rope project root folder: ')
        if self.project is not None:
            self.close_project()
        progress = self.env.create_progress('Opening [%s] project' % root)
        self.project = rope.base.project.Project(root)
        if self.env.get('enable_autoimport'):
            underlined = self.env.get('autoimport_underlineds')
            self.autoimport = autoimport.AutoImport(self.project,
                                                    underlined=underlined)
        progress.done()

    @decorators.global_command('k')
    def close_project(self):
        if self.project is not None:
            progress = self.env.create_progress('Closing [%s] project' %
                                                self.project.address)
            self.project.close()
            self.project = None
            progress.done()

    @decorators.global_command()
    def write_project(self):
        if self.project is not None:
            progress = self.env.create_progress(
                'Writing [%s] project data to disk' % self.project.address)
            self.project.sync()
            progress.done()

    @decorators.global_command('u')
    def undo(self):
        self._check_project()
        change = self.project.history.tobe_undone
        if change is None:
            self.env.message('Nothing to undo!')
            return
        if self.env.y_or_n('Undo [%s]? ' % str(change)):
            def undo(handle):
                for changes in self.project.history.undo(task_handle=handle):
                    self._reload_buffers(changes, undo=True)
            refactor.runtask(self.env, undo, 'Undo refactoring',
                             interrupts=False)

    @decorators.global_command('r')
    def redo(self):
        self._check_project()
        change = self.project.history.tobe_redone
        if change is None:
            self.env.message('Nothing to redo!')
            return
        if self.env.y_or_n('Redo [%s]? ' % str(change)):
            def redo(handle):
                for changes in self.project.history.redo(task_handle=handle):
                    self._reload_buffers(changes)
            refactor.runtask(self.env, redo, 'Redo refactoring',
                             interrupts=False)

    @decorators.local_command('a g', shortcut='C-c g')
    def goto_definition(self):
        self._check_project()
        resource, offset = self._get_location()
        maxfixes = self.env.get('codeassist_maxfixes')
        definition = codeassist.get_definition_location(
            self.project, self._get_text(), offset, resource, maxfixes)
        if tuple(definition) != (None, None):
            self.env.push_mark()
            self._goto_location(definition[0], definition[1])
        else:
            self.env.message('Cannot find the definition!')

    @decorators.local_command('a d', 'P', 'C-c d')
    def show_doc(self, prefix):
        self._check_project()
        self._base_show_doc(prefix, codeassist.get_doc)

    @decorators.local_command('a c', 'P')
    def show_calltip(self, prefix):
        self._check_project()
        def _get_doc(project, text, offset, *args, **kwds):
            try:
                offset = text.rindex('(', 0, offset) - 1
            except ValueError:
                return None
            return codeassist.get_calltip(project, text, offset, *args, **kwds)
        self._base_show_doc(prefix, _get_doc)
            
    def _base_show_doc(self, prefix, get_doc):
        maxfixes = self.env.get('codeassist_maxfixes')
        text = self._get_text()
        offset = self.env.get_offset()
        docs = get_doc(self.project, text, offset,
                       self._get_resource(), maxfixes)
        self.env.show_doc(docs, prefix)
        if docs is None:
            self.env.message('No docs avilable!')

    def _get_text(self):
        if not self.env.is_modified():
            return self._get_resource().read()
        return self.env.get_text()

    def _base_findit(self, do_find, optionals, get_kwds):
        self._check_project()
        self._save_buffers()
        resource, offset = self._get_location()

        action, values = dialog.show_dialog(
            self._askdata, ['search', 'cancel'], optionals=optionals)
        if action == 'search':
            kwds = get_kwds(values)
            def calculate(handle):
                resources = refactor._resources(self.project,
                                                values.get('resources'))
                return do_find(self.project, resource, offset,
                               resources=resources, task_handle=handle, **kwds)
            result = refactor.runtask(self.env, calculate, 'Find Occurrences')
            locations = [Location(location) for location in result]
            self.env.show_occurrences(locations)

    @decorators.local_command('a f', shortcut='C-c f')
    def find_occurrences(self):
        optionals = {
            'unsure': dialog.Data('Find uncertain occurrences: ',
                                  default='no', values=['yes', 'no']),
            'resources': dialog.Data('Files to search: '),
            'in_hierarchy': dialog.Data(
                    'Rename methods in class hierarchy: ',
                    default='no', values=['yes', 'no'])}
        def get_kwds(values):
            return {'unsure': values.get('unsure') == 'yes',
                    'in_hierarchy': values.get('in_hierarchy') == 'yes'}
        self._base_findit(findit.find_occurrences, optionals, get_kwds)

    @decorators.local_command('a i')
    def find_implementations(self):
        optionals = {'resources': dialog.Data('Files to search: ')}
        def get_kwds(values):
            return {}
        self._base_findit(findit.find_implementations, optionals, get_kwds)

    @decorators.local_command('a /', 'P', 'M-/')
    def code_assist(self, prefix):
        _CodeAssist(self, self.env).code_assist(prefix)

    @decorators.local_command('a ?', 'P', 'M-?')
    def lucky_assist(self, prefix):
        _CodeAssist(self, self.env).lucky_assist(prefix)

    @decorators.local_command()
    def get_completion_candidates(self):
        return _CodeAssist(self, self.env)._calculate_proposals()

    @decorators.local_command()
    def get_documentation(self,name):
        return _CodeAssist(self, self.env).get_documentation(name)
    
    @decorators.local_command()
    def auto_import(self):
        _CodeAssist(self, self.env).auto_import()

    def _check_autoimport(self):
        self._check_project()
        if self.autoimport is None:
            self.env.message('autoimport is disabled; '
                             'see `enable_autoimport\' variable')
            return False
        return True

    @decorators.global_command()
    def generate_autoimport_cache(self):
        if not self._check_autoimport():
            return
        modules = self.env.get('autoimport_modules')
        modnames = []
        if modules:
            for i in range(len(modules)):
                modname = modules[i]
                if not isinstance(modname, basestring):
                    modname = modname.value()
                modnames.append(modname)
        def generate(handle):
            self.autoimport.generate_cache(task_handle=handle)
            self.autoimport.generate_modules_cache(modules, task_handle=handle)
        refactor.runtask(self.env, generate, 'Generate autoimport cache')

    @decorators.global_command('f', 'P')
    def find_file(self, prefix):
        file = self._base_find_file(prefix)
        if file is not None:
            self.env.find_file(file.real_path)

    @decorators.global_command('4 f', 'P')
    def find_file_other_window(self, prefix):
        file = self._base_find_file(prefix)
        if file is not None:
            self.env.find_file(file.real_path, other=True)

    def _base_find_file(self, prefix):
        self._check_project()
        if prefix:
            files = self.project.pycore.get_python_files()
        else:
            files = self.project.get_files()
        return self._ask_file(files)

    def _ask_file(self, files):
        names = []
        for file in files:
            names.append('<'.join(reversed(file.path.split('/'))))
        result = self.env.ask_values('Rope Find File: ', names)
        if result is not None:
            path = '/'.join(reversed(result.split('<')))
            file = self.project.get_file(path)
            return file
        self.env.message('No file selected')

    @decorators.local_command('a j')
    def jump_to_global(self):
        if not self._check_autoimport():
            return
        all_names = list(self.autoimport.get_all_names())
        name = self.env.ask_values('Global name: ', all_names)
        result = dict(self.autoimport.get_name_locations(name))
        if len(result) == 1:
            resource = list(result.keys())[0]
        else:
            resource = self._ask_file(result.keys())
        if resource:
            self._goto_location(resource, result[resource])

    @decorators.global_command('c')
    def project_config(self):
        self._check_project()
        if self.project.ropefolder is not None:
            config = self.project.ropefolder.get_child('config.py')
            self.env.find_file(config.real_path)
        else:
            self.env.message('No rope project folder found')

    @decorators.global_command('n m')
    def create_module(self):
        def callback(sourcefolder, name):
            return generate.create_module(self.project, name, sourcefolder)
        self._create('module', callback)

    @decorators.global_command('n p')
    def create_package(self):
        def callback(sourcefolder, name):
            folder = generate.create_package(self.project, name, sourcefolder)
            return folder.get_child('__init__.py')
        self._create('package', callback)

    @decorators.global_command('n f')
    def create_file(self):
        def callback(parent, name):
            return parent.create_file(name)
        self._create('file', callback, 'parent')

    @decorators.global_command('n d')
    def create_directory(self):
        def callback(parent, name):
            parent.create_folder(name)
        self._create('directory', callback, 'parent')

    @decorators.local_command()
    def analyze_module(self):
        """Perform static object analysis on this module"""
        self._check_project()
        resource = self._get_resource()
        self.project.pycore.analyze_module(resource)

    @decorators.global_command()
    def analyze_modules(self):
        """Perform static object analysis on all project modules"""
        self._check_project()
        def _analyze_modules(handle):
            libutils.analyze_modules(self.project, task_handle=handle)
        refactor.runtask(self.env, _analyze_modules, 'Analyze project modules')

    @decorators.local_command()
    def run_module(self):
        """Run and perform dynamic object analysis on this module"""
        self._check_project()
        resource = self._get_resource()
        process = self.project.pycore.run_module(resource)
        try:
            process.wait_process()
        finally:
            process.kill_process()

    def _create(self, name, callback, parentname='source'):
        self._check_project()
        confs = {'name': dialog.Data(name.title() + ' name: ')}
        parentname = parentname + 'folder'
        optionals = {parentname: dialog.Data(
                parentname.title() + ' Folder: ',
                default=self.project.address, kind='directory')}
        action, values = dialog.show_dialog(
            self._askdata, ['perform', 'cancel'], confs, optionals)
        if action == 'perform':
            parent = libutils.path_to_resource(
                self.project, values.get(parentname, self.project.address))
            resource = callback(parent, values['name'])
            if resource:
                self.env.find_file(resource.real_path)

    def _goto_location(self, resource, lineno):
        if resource:
            self.env.find_file(str(resource.real_path),
                               resource.project != self.project)
        if lineno:
            self.env.goto_line(lineno)

    def _get_location(self):
        resource = self._get_resource()
        offset = self.env.get_offset()
        return resource, offset

    def _get_resource(self, filename=None):
        if filename is None:
            filename = self.env.filename()
        if filename is None:
            return
        resource = libutils.path_to_resource(self.project, filename, 'file')
        return resource

    def _check_project(self):
        if self.project is None:
            if self.env.get('guess_project'):
                self.open_project(self._guess_project())
            else:
                self.open_project()
        else:
            self.project.validate(self.project.root)

    def _guess_project(self):
        cwd = self.env.filename()
        if cwd is not None:
            while True:
                ropefolder = os.path.join(cwd, '.ropeproject')
                if os.path.exists(ropefolder) and os.path.isdir(ropefolder):
                    return cwd
                newcwd = os.path.dirname(cwd)
                if newcwd == cwd:
                    break
                cwd = newcwd

    def _reload_buffers(self, changes, undo=False):
        self._reload_buffers_for_changes(
            changes.get_changed_resources(),
            self._get_moved_resources(changes, undo))

    def _reload_buffers_for_changes(self, changed, moved={}):
        filenames = [resource.real_path for resource in changed]
        moved = dict([(resource.real_path, moved[resource].real_path)
                      for resource in moved])
        self.env.reload_files(filenames, moved)

    def _get_moved_resources(self, changes, undo=False):
        result = {}
        if isinstance(changes, rope.base.change.ChangeSet):
            for change in changes.changes:
                result.update(self._get_moved_resources(change))
        if isinstance(changes, rope.base.change.MoveResource):
            result[changes.resource] = changes.new_resource
        if undo:
            return dict([(value, key) for key, value in result.items()])
        return result

    def _save_buffers(self, only_current=False):
        if only_current:
            filenames = [self.env.filename()]
        else:
            filenames = self.env.filenames()
        pythons = []
        for filename in filenames:
            if self._is_python_file(filename):
                pythons.append(filename)
        self.env.save_files(pythons)

    def _is_python_file(self, path):
        resource = self._get_resource(path)
        return (resource is not None and
                resource.project == self.project and
                self.project.pycore.is_python_file(resource))

    def _askdata(self, data, starting=None):
        ask_func = self.env.ask
        ask_args = {'prompt': data.prompt, 'starting': starting,
                    'default': data.default}
        if data.values:
            ask_func = self.env.ask_values
            ask_args['values'] = data.values
        elif data.kind == 'directory':
            ask_func = self.env.ask_directory
        return ask_func(**ask_args)


class Location(object):
    def __init__(self, location):
        self.location = location
        self.filename = location.resource.real_path
        self.offset = location.offset
        self.note = ''
        if location.unsure:
            self.note = '?'

    @property
    def lineno(self):
        if hasattr(self.location, 'lineno'):
            return self.location.lineno
        return self.location.resource.read().count('\n', 0, self.offset) + 1


class _CodeAssist(object):

    def __init__(self, interface, env):
        self.interface = interface
        self.autoimport = interface.autoimport
        self.env = env
        self._source = None
        self._offset = None
        self._starting_offset = None
        self._starting = None
        self._expression = None

    def code_assist(self, prefix):
        names = self._calculate_proposals()
        if prefix is not None:
            arg = self.env.prefix_value(prefix)
            if arg == 0:
                arg = len(names)
            common_start = self._calculate_prefix(names[:arg])
            self.env.insert(common_start[self.offset - self.starting_offset:])
            self._starting = common_start
            self._offset = self.starting_offset + len(common_start)
        prompt = 'Completion for %s: ' % self.expression
        result = self.env.ask_completion(prompt, names, self.starting)
        if result is not None:
            self._apply_assist(result)

    def lucky_assist(self, prefix):
        names = self._calculate_proposals()
        selected = 0
        if prefix is not None:
            selected = self.env.prefix_value(prefix)
        if 0 <= selected < len(names):
            result = names[selected]
        else:
            self.env.message('Not enough proposals!')
            return
        self._apply_assist(result)

    def auto_import(self):
        if not self.interface._check_autoimport():
            return
        name = self.env.current_word()
        modules = self.autoimport.get_modules(name)
        if modules:
            if len(modules) == 1:
                module = modules[0]
            else:
                module = self.env.ask_values(
                    'Which module to import: ', modules)
            self._insert_import(name, module)
        else:
            self.env.message('Global name %s not found!' % name)

    def _apply_assist(self, assist):
        if ' : ' in assist:
            name, module = assist.rsplit(' : ', 1)
            self.env.delete(self.starting_offset + 1, self.offset + 1)
            self.env.insert(name)
            self._insert_import(name, module)
        else:
            self.env.delete(self.starting_offset + 1, self.offset + 1)
            self.env.insert(assist)

    def _calculate_proposals(self):
        self.interface._check_project()
        resource = self.interface._get_resource()
        maxfixes = self.env.get('codeassist_maxfixes')
        proposals = codeassist.code_assist(
            self.interface.project, self.source, self.offset,
            resource, maxfixes=maxfixes)
        proposals = codeassist.sorted_proposals(proposals)
        names = [proposal.name for proposal in proposals]
        if self.autoimport is not None:
            if self.starting.strip() and '.' not in self.expression:
                import_assists = self.autoimport.import_assist(self.starting)
                names.extend(x[0] + ' : ' + x[1] for x in import_assists)
        return names

    def _insert_import(self, name, module):
        lineno = self.autoimport.find_insertion_line(self.source)
        line = 'from %s import %s' % (module, name)
        self.env.insert_line(line, lineno)

    def _calculate_prefix(self, names):
        if not names:
            return ''
        prefix = names[0]
        for name in names:
            common = 0
            for c1, c2 in zip(prefix, name):
                if c1 != c2 or ' ' in (c1, c2):
                    break
                common += 1
            prefix = prefix[:common]
        return prefix

    def get_documentation(self,name):
        sourceCopyStart = self.source[:self.starting_offset]
        sourceCopyEnd = self.source[min(len(self.source)-1,self.offset+1):]
        sourceCopy = sourceCopyStart + name + sourceCopyEnd
        maxfixes = self.env.get('codeassist_maxfixes')
        offset = self.starting_offset + len(name)
        res  = self.interface._get_resource()
        docs = codeassist.get_doc(self.interface.project,
                                  sourceCopy,
                                  offset, res,
                                  maxfixes)
        return docs
    
    @property
    def offset(self):
        if self._offset is None:
            self._offset = self.env.get_offset()
        return self._offset

    @property
    def source(self):
        if self._source is None:
            self._source = self.interface._get_text()
        return self._source

    @property
    def starting_offset(self):
        if self._starting_offset is None:
            self._starting_offset = codeassist.starting_offset(self.source,
                                                               self.offset)
        return self._starting_offset

    @property
    def starting(self):
        if self._starting is None:
            self._starting = self.source[self.starting_offset:self.offset]
        return self._starting

    @property
    def expression(self):
        if self._expression is None:
            self._expression = codeassist.starting_expression(self.source,
                                                              self.offset)
        return self._expression
