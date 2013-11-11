# vim: set ts=8 sts=2 sw=2 tw=99 et:
#
# This file is part of AMBuild.
# 
# AMBuild is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
# 
# AMBuild is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
# 
# You should have received a copy of the GNU General Public License
# along with AMBuild. If not, see <http://www.gnu.org/licenses/>.
import os
from ... import util
from ... import nodetypes
from .. cpp import DetectCompiler
from .. import base_gen
from ... import database

class CppNodes(object):
  def __init__(self, output, debug_outputs):
    self.binary = output
    self.debug = debug_outputs

class Generator(base_gen.Generator):
  def __init__(self, sourcePath, buildPath, options, args, db=None):
    super(Generator, self).__init__(sourcePath, buildPath, options, args)
    self.cacheFolder = os.path.join(self.buildPath, '.ambuild2')
    self.old_scripts_ = set()
    self.old_folders_ = set()
    self.old_commands_ = set()
    self.old_groups_ = set()
    self.bad_outputs_ = set()
    self.bad_folders_ = set()
    self.db = db
    self.is_bootstrap = not self.db

  def preGenerate(self):
    if not os.path.isdir(self.cacheFolder):
      os.mkdir(self.cacheFolder)
    if not self.db:
      self.db = database.Database(os.path.join(self.cacheFolder, 'graph'))
      self.db.connect()
    self.db.create_tables()

    self.db.query_scripts(lambda id,path,stamp: self.old_scripts_.add(path))
    self.db.query_mkdir(lambda entry: self.old_folders_.add(entry))
    self.db.query_commands(lambda entry: self.old_commands_.add(entry))
    self.db.query_groups(lambda entry:self.old_groups_.add(entry))

  def cleanup(self):
    for cmd_entry in self.old_commands_:
      self.db.drop_command(cmd_entry)

    for path in self.old_scripts_:
      self.db.drop_script(path)

    for group in self.old_groups_:
      self.db.drop_group(group)

    class Node:
      def __init__(self):
        self.incoming = set()
        self.outgoing = set()

    # Build a tree of dead folders.
    tracker = {}
    for entry in self.old_folders_:
      if entry not in tracker:
        tracker[entry] = Node()

      if entry.folder is None:
        continue

      if entry.folder not in tracker:
        tracker[entry.folder] = Node()

      parent = tracker[entry.folder]
      child = tracker[entry]
      parent.incoming.add(entry)
      child.outgoing.add(entry.folder)

    # Find the leaves. Sets start out >= 1 items. Remove them as they they
    # are empty.
    dead_folders = [entry for entry in self.old_folders_ if len(tracker[entry].incoming) == 0]
    while len(dead_folders):
      child_entry = dead_folders.pop()
      child_node = tracker[child_entry]

      self.db.drop_folder(child_entry)
      for parent_entry in child_node.outgoing:
        parent_node = tracker[parent_entry]
        parent_node.incoming.remove(child_entry)
        if not len(parent_node.incoming):
          dead_folders.append(parent_entry)

  def postGenerate(self):
    self.cleanup()
    self.db.commit()
    if self.is_bootstrap:
      self.saveVars()
      self.db.close()

  def saveVars(self):
    vars = {
      'sourcePath': self.sourcePath,
      'buildPath': self.buildPath,
      'options': self.options,
      'args': self.args
    }
    with open(os.path.join(self.cacheFolder, 'vars'), 'wb') as fp:
      util.DiskPickle(vars, fp)


  def getLocalFolder(self, context):
    if type(context.localFolder_) is nodetypes.Entry or context.localFolder_ is None:
      return context.localFolder_

    if len(context.buildFolder):
      context.localFolder_ = self.generateFolder(None, context.buildFolder)
    else:
      context.localFolder_ = None
    return context.localFolder_

  def generateFolder(self, parent, folder):
    parent_path = ''
    if parent:
      parent_path = parent.path
    path = os.path.normpath(os.path.join(parent_path, folder))

    if path.startswith('..'):
      util.con_err(
        util.ConsoleRed,
        'Output path ',
        util.ConsoleBlue,
        path,
        util.ConsoleRed,
        ' is outside the build folder!',
        util.ConsoleNormal
      )
      raise Exception('Cannot generate folders outside the build folder')

    # Quick check. If this folder is not in our old folder list, and it's in
    # the DB, then we already have an entry for it that has fixed up its
    # parent paths.
    old_entry = self.db.query_path(path)
    if old_entry and old_entry not in self.old_folders_:
      # We have to make sure it's actually a folder entry, or if it isn't,
      # it's something we will try to fix up to a folder later.
      if old_entry.type == nodetypes.Mkdir or old_entry in self.bad_folders_:
        return old_entry

    components = []
    while folder:
      folder, name = os.path.split(folder)
      if not name:
        break
      components.append(name)

    path = parent_path
    while len(components):
      name = components.pop()
      path = os.path.join(path, name)
      entry = self.db.query_path(path)
      if not entry:
        entry = self.db.add_folder(parent, path)
      else:
        # We let the same folder be generated twice, so use discard, not remove.
        self.old_folders_.discard(entry)

        # If the old entry is not an output/mkdir, error.
        if entry.type != nodetypes.Mkdir and entry.type != nodetypes.Output:
          util.con_err(
            util.ConsoleRed,
            'Folder has the same node signature as: ',
            util.ConsoleBlue,
            entry.format(),
            util.ConsoleNormal
          )
          raise Exception('Output has been duplicated: {0}'.format(entry.path))

        # If the old entry is an output, we can tell if it's unused later.
        # :TODO:

      parent = entry

    return entry

  def addCommand(self, context, node_type, folder, data, inputs, outputs, weak_inputs=[]):
    if not folder:
      folder = context.localFolder
    assert not folder or type(folder) is nodetypes.Entry

    # Build the set of weak links.
    weak_links = set()
    for weak_input in weak_inputs:
      assert type(weak_input) is nodetypes.Entry
      assert weak_input.type != nodetypes.Source
      weak_links.add(weak_input)

    # Build the set of strong links.
    strong_links = set()
    for strong_input in inputs:
      if type(strong_input) is str:
        strong_input = self.db.find_or_add_source(strong_input)
      strong_links.add(strong_input)

    cmd_entry = None
    output_nodes = []
    for output in outputs:
      output_node = self.db.query_relpath(folder, output)
      if not output_node:
        output_node = self.db.add_output(folder, output)
      else:
        if output_node.type != nodetypes.Output:
          if output_node.type != nodetypes.Mkdir or \
             output_node not in self.old_folders_ or \
             output_node in self.bad_outputs_:
            type_string = nodetypes.NodeNames[output_node.type]
            raise Exception('Output already exists as node type: {0}'.format(type_string))

          # The output_node mkdir might be removed later, so remember to recheck.
          self.bad_outputs_.add(output_node)

        incoming = self.db.query_strong_inputs(output_node)

        if len(incoming):
          assert len(incoming) == 1

          input_entry = list(incoming)[0]
          assert input_entry.isCommand()

          # Make sure this output won't be duplicated.
          if input_entry not in self.old_commands_:
            util.con_err(
              util.ConsoleRed,
              'Command: ',
              input_entry.format(),
              util.ConsoleNormal
            )
            raise Exception('Output has been duplicated: {0}'.format(output_node.path))

          if not cmd_entry:
            cmd_entry = input_entry

      output_nodes.append(output_node)
    # end for

    must_link = set(output_nodes)

    if cmd_entry:
      # Update the entry in the database.
      self.db.update_command(cmd_entry, node_type, folder, data)

      # Disconnect any outputs that are no longer connected to this output.
      # It's okay to use must_link since there should never be duplicate
      # outputs.
      for outgoing in self.db.query_strong_outgoing(cmd_entry):
        if outgoing not in must_link:
          self.db.drop_strong_edge(cmd_entry, outgoing)
          self.old_outputs.add(outgoing)
        else:
          must_link.remove(outgoing)

      # Remove us from the list of commands to delete.
      self.old_commands_.remove(cmd_entry)
    else:
      cmd_entry = self.db.add_command(node_type, folder, data)

    # Connect each output.
    for output_node in must_link:
      self.db.add_strong_edge(cmd_entry, output_node)

    # Connect/disconnect strong inputs.
    strong_inputs = self.db.query_strong_inputs(cmd_entry)
    strong_added = strong_links - strong_inputs
    strong_removed = strong_inputs - strong_links 
    for strong_input in strong_added:
      self.db.add_strong_edge(strong_input, cmd_entry)
    for strong_input in strong_removed:
      self.db.drop_strong_edge(strong_input, cmd_entry)

    # Connect/disconnect weak inputs.
    weak_inputs = self.db.query_weak_inputs(cmd_entry)
    weak_added = weak_links - weak_inputs
    weak_removed = weak_inputs - weak_links 
    for weak_input in weak_added:
      self.db.add_weak_edge(weak_input, cmd_entry)
    for weak_input in weak_removed:
      self.db.drop_weak_edge(weak_input, cmd_entry)

    # If we got new outputs or inputs, we need to re-run the command.
    changed = len(must_link) + len(strong_added) + len(weak_added)
    if changed and not cmd_entry.dirty:
      self.db.mark_dirty(cmd_entry)

    return cmd_entry, output_nodes

  def addCxxTasks(self, cx, binary):
    folder_node = self.generateFolder(cx.localFolder, binary.localFolder)

    inputs = []

    # Find dependencies
    for item in binary.compiler.linkflags:
      if type(item) is str:
        continue
      inputs.append(item.node)

    for item in binary.compiler.postlink:
      if type(item) is str:
        node = self.graph.depNodeForPath(item)
      else:
        node = item.node
      inputs.append(node)

    for objfile in binary.objfiles:
      cxxData = {
        'argv': objfile.argv,
        'type': binary.linker.behavior
      }
      cxxCmd, (cxxNode,) = self.addCommand(
        context=cx,
        weak_inputs=binary.compiler.sourcedeps,
        inputs=[objfile.sourceFile],
        outputs=[objfile.outputFile],
        node_type=nodetypes.Cxx,
        folder=folder_node,
        data=cxxData
      )
      inputs.append(cxxNode)

    outputs = [binary.outputFile]
    if binary.pdbFile:
      outputs.append(binary.pdbFile)
  
    linkCmd, binNodes = self.addCommand(
      context=cx,
      node_type=nodetypes.Command,
      folder=folder_node,
      data=binary.argv,
      inputs=inputs,
      outputs=outputs
    )

    return CppNodes(binNodes[0], binNodes[1:])

  @staticmethod
  def getPathInContext(context, node):
    if node.type == nodetypes.Source:
      assert os.path.isabs(node.path)
      return node.path
    
    assert not os.path.isabs(node.path)
    assert node.type == nodetypes.Output
    return os.path.relpath(node.path, context.buildFolder)

  @staticmethod
  def find_folder(context, path):
    if path[-1] == os.sep or path[-1] == os.altsep:
      return path
    if os.path.normpath(path) == '.':
      return '.' + os.sep
    path = os.path.normpath(os.path.join(context.buildFolder, path))
    return self.db.query_path(path)

  # def outputFolderInContext(self, context, path):
  #   if output_path[-1] == os.sep or output_path[-1] == os.altsep:
  #     return os.path.join(context.buildFolder, output_path)
  #   if os.path.normpath(output_path) == '.':
  #     return context.buildFolder

  #   else:
  #     output_folder = os.path.join(context.buildFolder, output_path)
  #     output_folder = os.path.normpath(output_folder)
  #     output_entry = self.db.query_path(output_folder)
  #     if output_entry and output_entry.type != nodetypes.
  #       # If 

  #def addFileOp(self, cmd, context, source, output_path):
    # Find the folder the destination file will be in.

  def addSource(self, context, source_path):
    return self.graph.addSource(source_path)

  def addCopy(self, context, source, output_path):
    return self.addFileOp(nodetypes.Copy, context, source, output_path)

  def addSymlink(self, context, source, output_path):
    if util.IsWindows():
      # Windows pre-Vista does not support symlinks. Windows Vista+ supports
      # symlinks via mklink, but it's Administrator-only by default.
      return self.addFileOp(nodetypes.Copy, context, source, output_path)
    return self.addFileOp(nodetypes.Symlink, context, source, output_path)

  def addFolder(self, context, folder):
    return self.generateFolder(context.localFolder, folder)

  def addCopy(self, context, source, output_path):
    return self.graph.addCopy(context, source, output_path)

  def addShellCommand(self, context, inputs, argv, outputs):
    return self.addCommand(
      context=context,
      node_type=nodetypes.Command,
      folder=None,
      data=argv,
      inputs=inputs,
      outputs=outputs
    )

  def addConfigureFile(self, context, path):
    self.old_scripts_.discard(path)
    self.db.add_or_update_script(path)

