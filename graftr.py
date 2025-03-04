# Copyright 2020 LMNT, Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================

import numpy as np
import os
import re
import sys
import csv

from cmd import Cmd
from copy import deepcopy
from pprint import pprint


def _parse_device(device_spec):
    try:
        return torch.device(device_spec)
    except RuntimeError:
        return None


def _dirname(path):
    parts = re.sub(r'/+', '/', path).split('/')[:-1]
    if len(parts) == 1 and parts[0] == '':
        return '/'
    return '/'.join(parts)


def _basename(path):
    return re.sub(r'/+', '/', path).split('/')[-1]


def pred_dir(n, p):
    return n.is_directory


def pred_dir_or_tensor(n, p):
    return n.is_directory or isinstance(n.value, torch.Tensor)


class _Node:
    def __init__(self, parent, name):
        self.parent = parent or self
        self.name = name

    @property
    def full_name(self):
        parts = []
        while self != self.parent:
            parts.append(str(self.name))
            self = self.parent
        return '/' + '/'.join(reversed(parts))

    @property
    def is_root(self):
        return self == self.parent

    @property
    def is_directory(self):
        return False

    def clone(self, clone_parent, name=None):
        parent = self.parent
        self.parent = None
        clone = deepcopy(self)
        clone.parent = clone_parent
        self.parent = parent
        if name is not None:
            clone.name = name
        return clone


class _DirNode(_Node):
    def __init__(self, parent, name):
        super().__init__(parent, name)
        self.children = []

    def add_child(self, node):
        self.children.append(node)

    def child(self, name):
        for child in self.children:
            if str(child.name) == name:
                return child
        return None

    def value_nodes(self):
        ret = []
        for child in self.children:
            ret += child.value_nodes()
        return ret

    @property
    def is_directory(self):
        return True


class DictNode(_DirNode):
    def state_dict(self):
        ret = dict()
        for child in self.children:
            ret.update(child.state_dict())
        return ret if self == self.parent else {self.name: ret}


class PartialNode(_DirNode):
    def state_dict(self):
        ret = dict()
        for child in self.children:
            ret.update(child.state_dict())
        return {f'{self.name}.{key}': value for key, value in ret.items()}


class ValueNode(_Node):
    def __init__(self, parent, name, value):
        super().__init__(parent, name)
        self.value = value

    def child(self, name):
        return None

    def value_nodes(self):
        return [self]

    def state_dict(self):
        return {self.name: self.value}


class Tree:
    def __init__(self, state_dict):
        self.root = self._from_state_dict(state_dict)

    def insert(self, path, node):
        parts = path.split('/')
        cur = self.root
        for elem in parts:
            if elem == '':
                continue
            candidate = cur.child(elem)
            if candidate is None:
                candidate = PartialNode(cur, elem)
                cur.add_child(candidate)
            cur = candidate
        cur.add_child(node)
        return cur

    def resolve(self, path, node=None):
        parts = path.split('/')
        if node is None:
            node = self.root
        if len(path) > 0 and path[0] == '/':
            node = self.root
        for elem in parts:
            if elem == '' or elem == '.':
                pass
            elif elem == '..':
                node = node.parent
            else:
                node = node.child(elem)
                if node is None:
                    return None
        return node

    def resolve_path(self, path, node=None):
        parts = path.split('/')
        canonical_parts = []
        if (len(path) == 0 or path[0] != '/') and node is not None:
            parts = node.full_name.split('/')[1:] + parts
        for elem in parts:
            if elem == '' or elem == '.':
                pass
            elif elem == '..':
                if len(canonical_parts) > 0:
                    canonical_parts.pop()
            else:
                canonical_parts.append(elem)
        return '/' + '/'.join(canonical_parts)

    def _from_state_dict(self, state_dict):
        """
    Returns a root Node object with a complete tree hierarchy underneath it
    that mirrors the supplied `state_dict`.
    """
        root = DictNode(None, '')
        stack = [(value, key, root) for key, value in state_dict.items()]
        while len(stack) > 0:
            value, name, parent = stack.pop()
            if isinstance(value, dict):
                node = DictNode(parent, name)
                for name in value.keys():
                    stack.append((value[name], name, node))
            elif '.' in name:
                chunks = name.split('.')
                for chunk in chunks[:-1]:
                    next = parent.child(chunk)
                    if next is None:
                        next = PartialNode(parent, chunk)
                        parent.add_child(next)
                    else:
                        assert isinstance(next, PartialNode)
                    parent = next
                node = ValueNode(parent, chunks[-1], value)
            else:
                node = ValueNode(parent, name, value)
            parent.add_child(node)
        return root


class CheckpointShell(Cmd):
    intro = 'Type help or ? to list commands.\n'
    prompt = '> '
    file = None

    # Hack to handle CTRL+C. Shamelessly stolen from StackOverflow:
    # https://stackoverflow.com/questions/8813291/better-handling-of-keyboardinterrupt-in-cmd-cmd-command-line-interpreter
    def cmdloop(self, intro=None):
        print(self.intro)
        while True:
            try:
                super().cmdloop(intro='')
                break
            except KeyboardInterrupt:
                print('^C')

    def default(self, line):
        print(f'{line.split()[0]}: unknown command.')

    def __init__(self, checkpoint_path):
        super().__init__()
        if os.path.islink(checkpoint_path):
            checkpoint_path = os.path.realpath(checkpoint_path)
            print(f'WARNING: following symbolic link to \'{checkpoint_path}\'.')
        self._path = checkpoint_path
        self._state = self.load_checkpoint_path(checkpoint_path)
        self._tree = Tree(self._state)
        self._cwd = self._tree.root
        self._prevwd = '/'
        self._dirty = False
        print(f'Checkpoint loaded from {checkpoint_path}.')

    def rename_multiple(self, rename_csv_path):
        with open(rename_csv_path, "r") as f:
            csv_reader = csv.reader(f, delimiter=",")
            for row in csv_reader:
                assert len(row) == 2
                self.do_mv(f"{row[0]} {row[1]}")
        self.do_save(self._path)

    def load_checkpoint_path(self, checkpoint_path):
        if torch.cuda.is_available():
            return torch.load(checkpoint_path)
        else:
            print(f'WARNING: Your environment is cpu-only. '
                  f'Do you want to load parameters in \'device:cpu\' (y/N)? ', end='', flush=True)
            line = sys.stdin.readline().strip().lower()
            if line != 'y' and line != 'yes':
                # raise RuntimeError and Trackback to show error and terminate command-line.
                return torch.load(checkpoint_path)
            print("")
            self._dirty = True
            return torch.load(checkpoint_path,
                              map_location=torch.device('cpu'))

    def help_shape(self):
        print('shape - print the tensor shape.')
        print('Syntax: shape TENSOR')

    def complete_shape(self, text, line, begidx, endidx):
        return self._complete_path(line[len('shape'):].strip(), pred_dir_or_tensor)

    def do_shape(self, arg):
        path = arg.strip()
        node = self._tree.resolve(path, self._cwd)
        if node is None:
            print(f'shape: \'{path}\' not found.')
            return
        if node.is_directory:
            print(f'shape: \'{path}\' is a directory, not a tensor.')
            return
        if not isinstance(node.value, torch.Tensor):
            print(f'shape: \'{path}\' is not a tensor.')
        print(list(node.value.shape))

    def help_parameters(self):
        print('parameters - print the number of model parameters under a directory.')
        print('Syntax: parameters [PATH]')
        print('Note: buffers and non-trainable parameters will be included in this count.')
        print('From PyTorch 1.6 onwards, consider using non-persistent buffers when possible.')

    def complete_parameters(self, text, line, begidx, endidx):
        return self._complete_path(line[len('parameters'):].strip(), pred_dir_or_tensor)

    def do_parameters(self, arg):
        path = arg.strip()
        node = self._tree.resolve(path, self._cwd)
        if node is None:
            print(f'parameters: \'{path}\' not found.')
            return
        count = 0
        for n in node.value_nodes():
            if isinstance(n.value, torch.Tensor):
                count += np.prod(n.value.shape)
        print(f'{int(count):,}')

    def help_pwd(self):
        print('pwd - print working directory.')
        print('Syntax: pwd')

    def do_pwd(self, arg):
        print(self._cwd.full_name)

    def help_cd(self):
        print('cd - change working directory.')
        print('Syntax: cd DIR')

    def complete_cd(self, text, line, begidx, endidx):
        return self._complete_path(line[len('cd'):].strip(), pred_dir)

    def do_cd(self, arg):
        if arg == '-':
            arg = self._prevwd
        node = self._tree.resolve(arg, self._cwd)
        if node is None:
            print(f'{arg}: not found.')
        elif not node.is_directory:
            print(f'{arg}: not a directory.')
        elif self._cwd != node:
            self._prevwd = self._cwd.full_name
            self._cwd = node

    def help_ls(self):
        print('ls - list directory contents.')
        print('Syntax: ls [PATH]')

    def complete_ls(self, text, line, begidx, endidx):
        return self._complete_path(line[len('ls'):].strip())

    def do_ls(self, arg):
        node = self._tree.resolve(arg.strip(), self._cwd)
        if node is None:
            print(f'ls: \'{arg}\' not found.')
        elif not node.is_directory:
            print(node.full_name)
        else:
            for child in sorted(node.children, key=lambda x: x.name):
                name = str(child.name) + ('/' if child.is_directory else '')
                print(name)

    def help_cat(self):
        print('cat - print the contents of a value or directory.')
        print('Syntax: cat PATH')

    def complete_cat(self, text, line, begidx, endidx):
        return self._complete_path(line[len('cat'):].strip())

    def do_cat(self, arg):
        node = self._tree.resolve(arg, self._cwd)
        if node is None:
            print(f'{arg}: not found.')
        elif node.is_directory:
            pprint(node.state_dict())
        else:
            print(node.value)

    def help_device(self):
        print('device - get or set the device of a tensor or group of tensors.')
        print('Syntax: device PATH [DEVICE_STR]')
        print(
            'If PATH is a directory, all tensors under it (recursively) will have their device changed to DEVICE_STR.')
        print('DEVICE_STR is a device string you would use with torch.device(...).')
        print('Example: device /foo/bar/baz cuda:0')

    def complete_device(self, text, line, begidx, endidx):
        m = re.match(r'device(\s+[^\s]*)(\s+.*)?\s*$', line)
        if not m or m.group(2):
            return []
        return self._complete_path(line[m.start(1):].strip(), pred_dir_or_tensor)

    def do_device(self, arg):
        m = re.match(r'([^\s]+)(\s+[^\s]+)?\s*$', arg)
        if not m:
            print(f'device: invalid usage.')
            return

        node = self._tree.resolve(m.group(1), self._cwd)
        if node is None:
            print(f'{m.group(1)}: invalid path.')
            return

        if not m.group(2):
            for v in node.value_nodes():
                if isinstance(v.value, torch.Tensor):
                    print(f'{v.full_name} : {v.value.device}')
            return

        device = _parse_device(m.group(2).strip())
        if device is None:
            print(f'{m.group(2).strip()}: invalid device specification.')
            return

        for v in node.value_nodes():
            if isinstance(v.value, torch.Tensor):
                print(f'{v.full_name} -> {device}')
                v.value = v.value.to(device)
                self._dirty = True

    def help_mv(self):
        print('mv - move/rename value or directory.')
        print('Syntax: mv SRC DEST')

    def complete_mv(self, text, line, begidx, endidx):
        m = re.match(r'mv(\s+[^\s]*)(\s+[^\s]*)?\s*$', line)
        if not m:
            return []
        start = m.start(2) if m.group(2) else m.start(1)
        end = m.end(2) if m.group(2) else m.end(1)
        return self._complete_path(line[start:end].strip())

    def do_mv(self, arg):
        m = re.match(r'([^\s]+)\s+([^\s]+)\s*$', arg)
        if not m:
            print(f'mv: invalid usage.')
            return

        src_node = self._tree.resolve(m.group(1), self._cwd)
        dest = self._tree.resolve_path(m.group(2), self._cwd)
        dest_node = self._tree.resolve(dest, self._cwd)
        if src_node is None:
            print(f'mv: \'{m.group(1)}\' not found.')
            return
        if src_node.full_name == dest:
            return
        if (dest + '/').startswith(src_node.full_name + '/'):
            print(f'mv: cannot move \'{m.group(1)}\' to a subdirectory of itself, \'{m.group(2)}\'.')
            return
        if dest_node is not None:
            if not dest_node.is_directory:
                print(f'mv: cannot overwrite \'{m.group(2)}\' with \'{m.group(1)}\'.')
                return
            self._rm_node(src_node)
            dest_node.add_child(src_node)
            src_node.parent = dest_node
        else:
            dest_dir = _dirname(dest)
            dest_name = _basename(dest)
            self._rm_node(src_node)
            dest_node = self._tree.insert(dest_dir, src_node)
            src_node.name = dest_name
            src_node.parent = dest_node
        self._dirty = True

    def help_rm(self):
        print('rm - remove value or directory.')
        print('Syntax: rm PATH')

    def complete_rm(self, text, line, begidx, endidx):
        return self._complete_path(line[len('rm'):].strip())

    def do_rm(self, arg):
        m = re.match(r'([^\s]+)\s*$', arg)
        if not m:
            print(f'rm: invalid usage.')
            return
        node = self._tree.resolve(m.group(1), self._cwd)
        if node is None:
            print(f'rm: \'{m.group(1)}\' not found.')
            return
        if node.is_root:
            print(f'rm: cannot remove root directory.')
            return
        self._rm_node(node)
        self._dirty = True

    def help_cp(self):
        print('cp - copy value or directory.')
        print('Syntax: cp SRC DEST')

    def complete_cp(self, text, line, begidx, endidx):
        m = re.match(r'mv(\s+[^\s]*)(\s+[^\s]*)?\s*$', line)
        if not m:
            return []
        start = m.start(2) if m.group(2) else m.start(1)
        end = m.end(2) if m.group(2) else m.end(1)
        return self._complete_path(line[start:end].strip())

    def do_cp(self, arg):
        m = re.match(r'([^\s]+)\s+([^\s]+)\s*$', arg)
        if not m:
            print(f'cp: invalid usage.')
            return

        src_node = self._tree.resolve(m.group(1), self._cwd)
        dest = self._tree.resolve_path(m.group(2), self._cwd)
        dest_node = self._tree.resolve(dest, self._cwd)
        if src_node is None:
            print(f'cp: \'{m.group(1)}\' not found.')
            return
        if src_node.full_name == dest:
            return
        if (dest + '/').startswith(src_node.full_name + '/'):
            print(f'cp: cannot copy \'{m.group(1)}\' to a subdirectory of itself, \'{m.group(2)}\'.')
            return
        if dest_node is not None:
            if not dest_node.is_directory:
                print(f'cp: cannot overwrite \'{m.group(2)}\' with \'{m.group(1)}\'.')
                return
            clone_node = src_node.clone(clone_parent=dest_node)
            dest_node.add_child(clone_node)
        else:
            dest_dir = _dirname(dest)
            dest_name = _basename(dest)
            clone_node = src_node.clone(clone_parent=None, name=dest_name)
            dest_node = self._tree.insert(dest_dir, clone_node)
            clone_node.parent = dest_node
        self._dirty = True

    def help_save(self):
        print('save - write back changes to disk.')
        print('Syntax: save [PATH]')
        print('Writes back changes to the loaded file or PATH if specified.')
        print('Use the `where` command to see the default save path.')

    def do_save(self, arg):
        path = arg.strip() or self._path
        try:
            torch.save(self._tree.root.state_dict(), path)
            self._path = path
            self._dirty = False
            print(f'Saved to \'{self._path}\'.')
        except FileNotFoundError:
            print(f'save: directory containing \'{path}\' does not exist.')
        except PermissionError:
            print(f'save: permission denied saving to \'{path}\'.')
        except Exception as e:
            print(f'save: caught exception {e.__class__.__name__} when saving to \'{path}\'.')

    def help_where(self):
        print('where - print the location on disk where changes will be saved.')
        print('Syntax: where')

    def do_where(self, arg):
        print(self._path)

    def help_exit(self):
        print('exit - exits the shell.')
        print('Syntax: exit')

    def do_exit(self, arg):
        return self.do_EOF(1)

    def help_EOF(self):
        print('^D - exits the shell.')
        print('Syntax: ^D')

    def default(self, arg):
        eval_globals = {'np': np, 'torch': torch}
        eval_locals = dict()
        for node in self._cwd.children:
            if node.is_directory:
                continue
            if isinstance(node.value, torch.Tensor):
                eval_locals[node.name] = node.value.clone()
            else:
                eval_locals[node.name] = node.value
        try:
            value = eval(compile(arg, '<string>', 'single'), eval_globals, eval_locals)
            if value is not None:
                print(value)
            for key, value in eval_locals.items():
                child = self._cwd.child(key)
                if not child:
                    raise NameError(f'name \'{key}\' is not defined.')
                if isinstance(child.value, torch.Tensor):
                    self._dirty = self._dirty or id(child.value) != id(value) or torch.any(child.value != value)
                else:
                    self._dirty = self._dirty or id(child.value) != id(value) or child.value != value
                child.value = value
        except Exception as e:
            print(f'{e.__class__.__name__}: {e}')

    def do_EOF(self, arg):
        if not arg:
            print('exit')
        if self._dirty:
            print('WARNING: there are pending changes that have not been saved to disk. Discard (y/N)? ', end='',
                  flush=True)
            line = sys.stdin.readline().strip().lower()
            if line != 'y' and line != 'yes':
                return
        print()
        return True

    def _rm_node(self, node):
        node.parent.children.remove(node)
        node = node.parent
        while len(node.children) == 0 and isinstance(node, PartialNode) and not node.is_root:
            node.parent.children.remove(node)
            node = node.parent

    def _complete_path(self, path, predicate=lambda n, p: True):
        node = self._tree.resolve(_dirname(path), self._cwd)
        if node is None:
            return []
        prefix = _basename(path)
        return [n.name + ('/' if n.is_directory else '') for n in node.children if
                n.name.startswith(prefix) and predicate(n, prefix)]


if __name__ == '__main__':
    if len(sys.argv) != 3:
        print(f'Usage: {sys.argv[0]} CHECKPOINT_PATH RENAME_CSV_PATH')
    else:
        import torch

        with torch.no_grad():
            CheckpointShell(sys.argv[1]).rename_multiple(sys.argv[2])
