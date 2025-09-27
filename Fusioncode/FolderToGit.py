# ==== Fusion 360 Folder Exporter (3D Printing) ====
# Simple workflow:
# 1) Ask for local output directory
# 2) Pick a Fusion Project and Folder via dropdowns
# 3) Choose a single export format: 3MF, STL, or OBJ
# Exports all F3D/F3Z designs in the selected folder (including subfolders) to the chosen format.

import adsk.core, adsk.fusion, traceback, os

_app = None
_ui = None

# Helpers
def ensure_dir(path):
    if not os.path.exists(path):
        os.makedirs(path, exist_ok=True)

def _default_initial_dir():
    """Return a sensible starting directory for the folder picker.
    On Windows, prefer C:\\ if it exists; otherwise use the user home.
    """
    try:
        if os.name == 'nt':
            for drive in ['C:', 'D:', 'E:', 'F:', 'G:']:
                d = drive + '\\'
                if os.path.isdir(d):
                    return d
            return os.path.expanduser('~')
        return '/'
    except:
        return os.path.expanduser('~')
    

def list_hubs(data):
    hubs = data.dataHubs
    return [hubs.item(i) for i in range(hubs.count)]

def list_projects(data):
    projs = data.dataProjects
    return [projs.item(i) for i in range(projs.count)]

def build_folder_paths(project):
    """Return list of folder path strings: e.g. 'admin', 'admin/usbc', etc."""
    paths = ['(Project root)']
    root = project.rootFolder
    def walk(folder, prefix):
        subs = folder.dataFolders
        for i in range(subs.count):
            f = subs.item(i)
            p = f"{prefix}/{f.name}" if prefix else f.name
            paths.append(p)
            walk(f, p)
    walk(root, '')
    return paths

def show_paths(ui, paths):
    # no-op retained for compatibility, not used in simplified UI
    return

def find_folder_by_path(project, path_str):
    if not path_str.strip():
        return project.rootFolder
    folder = project.rootFolder
    parts = [p for p in path_str.split('/') if p.strip()]
    for p in parts:
        found = None
        subs = folder.dataFolders
        for i in range(subs.count):
            f = subs.item(i)
            if f.name == p:
                found = f
                break
        if not found:
            return None
        folder = found
    return folder

def _collect_all_brep_bodies(root_comp):
    """Collects all solid BRepBodies from the root component and all occurrences.
    Returns an adsk.core.ObjectCollection or None if none found.
    """
    try:
        bodies = adsk.core.ObjectCollection.create()
        # Top-level bodies
        try:
            for b in root_comp.bRepBodies:
                try:
                    if getattr(b, 'isSolid', True):
                        bodies.add(b)
                except:
                    bodies.add(b)
        except:
            pass
        # Bodies in all occurrences
        try:
            occs = root_comp.allOccurrences
            for i in range(occs.count):
                comp = occs.item(i).component
                for b in comp.bRepBodies:
                    try:
                        if getattr(b, 'isSolid', True):
                            bodies.add(b)
                    except:
                        bodies.add(b)
        except:
            pass
        return bodies if bodies.count > 0 else None
    except:
        return None

# Removed native 'Save as Mesh' automation helpers as we now rely on API-based 3MF export paths only.

def populate_folder_dropdown(inputs, project, curr_path):
    """Populate the folder dropdown with the current folder and its immediate subfolders.
    curr_path: '' means project root.
    """
    global _isUpdatingUI
    if _isUpdatingUI:
        return
    folderDD = adsk.core.DropDownCommandInput.cast(inputs.itemById('folderDD'))
    label = adsk.core.TextBoxCommandInput.cast(inputs.itemById('folderPathLabel'))
    # Resolve folder from path
    folder = project.rootFolder if not curr_path else find_folder_by_path(project, curr_path)
    # Clear items
    _isUpdatingUI = True
    try:
        try:
            while folderDD.listItems.count > 0:
                folderDD.listItems.item(0).deleteMe()
        except:
            pass
        # Add current folder sentinel and subfolders
        folderDD.listItems.add('(Current folder)', True)
        try:
            subs = folder.dataFolders if folder else None
            if subs:
                for i in range(subs.count):
                    f = subs.item(i)
                    folderDD.listItems.add(f.name, False)
        except:
            pass
        # Update label
        try:
            label.text = curr_path if curr_path else '(Project root)'
        except:
            pass
    finally:
        _isUpdatingUI = False

try:
    class _DataFileDownloadHandler(adsk.core.DataFileDownloadEventHandler):
        """Handler to write a DataFile's downloaded bytes to a target file path."""
        def __init__(self, out_path: str):
            super().__init__()
            self.out_path = out_path
            self.ok = False
            self.error = None

        def notify(self, args):
            try:
                e = adsk.core.DataFileDownloadEventArgs.cast(args)
                data = getattr(e, 'data', None)
                # Try a few common representations
                buf = None
                try:
                    if data is None:
                        buf = None
                    elif isinstance(data, (bytes, bytearray)):
                        buf = bytes(data)
                    elif hasattr(data, 'asArray'):
                        buf = bytes(data.asArray())
                    elif hasattr(data, 'readAll'):
                        buf = data.readAll()
                except Exception:
                    buf = None
                if buf is None:
                    raise RuntimeError('Download handler received no data buffer')
                ensure_dir(os.path.dirname(self.out_path))
                with open(self.out_path, 'wb') as f:
                    f.write(buf)
                self.ok = True
            except Exception as ex:
                self.error = str(ex)
except Exception:
    _DataFileDownloadHandler = None

def _export_drawing_to_dxf(app, doc, out_dxf_path):
    """Best-effort export of an open Drawing document to DXF. Returns True on success."""
    try:
        # Try obtaining a drawing product if available
        drawing_prod = None
        try:
            drawing_prod = doc.products.itemByProductType('DrawingProductType')
        except:
            drawing_prod = None

        # Candidate export managers to try
        candidates = []
        try:
            if drawing_prod and hasattr(drawing_prod, 'exportManager'):
                candidates.append(getattr(drawing_prod, 'exportManager', None))
        except:
            pass
        try:
            if hasattr(doc, 'exportManager'):
                candidates.append(getattr(doc, 'exportManager', None))
        except:
            pass
        try:
            if hasattr(app, 'exportManager'):
                candidates.append(getattr(app, 'exportManager', None))
        except:
            pass

        # Try known option creators
        for em in candidates:
            if not em:
                continue
            # createDXFExportOptions variants (names vary by build)
            for method_name in ('createDXFExportOptions', 'createDrawingDXFExportOptions'):
                try:
                    if hasattr(em, method_name):
                        method = getattr(em, method_name)
                        opts = None
                        # Try with just filename
                        try:
                            opts = method(out_dxf_path)
                        except:
                            opts = None
                        # Try with doc
                        if not opts:
                            try:
                                opts = method(doc, out_dxf_path)
                            except:
                                opts = None
                        # Try with product
                        if not opts and drawing_prod:
                            try:
                                opts = method(drawing_prod, out_dxf_path)
                            except:
                                opts = None
                        if opts:
                            try:
                                # Some options have a filename property
                                setattr(opts, 'filename', out_dxf_path)
                            except:
                                pass
                            try:
                                adsk.doEvents()
                            except:
                                pass
                            try:
                                em.execute(opts)
                                return True
                            except:
                                pass
                except:
                    pass

        # Direct export fallbacks on product/document
        try:
            if drawing_prod and hasattr(drawing_prod, 'exportToDXF'):
                drawing_prod.exportToDXF(out_dxf_path)
                return True
        except:
            pass
        try:
            if hasattr(doc, 'exportToDXF'):
                doc.exportToDXF(out_dxf_path)
                return True
        except:
            pass
    except:
        pass
    return False

def traverse_and_export(app, ui, folder, base_output, export_formats, overwrite=True, rel_path='', error_list=None, include_other_files=False, other_exts=None, manifest_list=None, export_drawing_dxf=False):
    """Traverse a Fusion 360 data folder and export all F3D/F3Z designs
    into base_output using one or more formats (e.g., ['3mf','stl','obj']).
    Recurses into subfolders, mirroring their relative paths.
    """
    # Normalize formats to a set of lowercase strings
    if isinstance(export_formats, (list, tuple, set)):
        fmts = {str(f).lower().strip() for f in export_formats if str(f).strip()}
    else:
        fmts = {str(export_formats).lower().strip()} if export_formats else set()
    exported = {'designs':0, 'stl':0, '3mf':0, 'obj':0, 'other':0, 'otherFound':0, 'skipped':0, 'errors':0, 'pdfFail':0}
    out_dir = os.path.join(base_output, rel_path) if rel_path else base_output
    ensure_dir(out_dir)

    for df in folder.dataFiles:
        opened_doc = None
        try:
            ext = (df.fileExtension or '').lower()
            if ext not in ('f3d', 'f3z'):
                # Optionally download non-design files (e.g., DXF/DWG/PDF/images)
                if include_other_files:
                    try:
                        out_path = os.path.join(out_dir, df.name)
                        # Filter by extensions if provided (consider both fileExtension and name-based extension)
                        allowed = None
                        try:
                            if other_exts:
                                allowed = {e.lower().lstrip('.').strip() for e in other_exts if str(e).strip()}
                        except:
                            allowed = None
                        base_ext = (os.path.splitext(df.name)[1][1:] or '').lower()
                        file_ext = (df.fileExtension or '').lower().lstrip('.')
                        if allowed:
                            if base_ext not in allowed and file_ext not in allowed:
                                exported['skipped'] += 1
                                continue

                        # Special handling: Fusion Drawing -> attempt DXF export if requested
                        is_drawing = (file_ext == 'f2d' or base_ext == 'f2d')
                        if is_drawing and export_drawing_dxf:
                            try:
                                dxf_path = os.path.join(out_dir, (os.path.splitext(df.name)[0] or df.name) + '.dxf')
                                if overwrite or not os.path.exists(dxf_path):
                                    # Open the drawing document and try best-effort DXF export
                                    doc_pdf = None
                                    try:
                                        doc_pdf = app.documents.open(df, True)
                                        try:
                                            doc_pdf.activate()
                                        except:
                                            pass
                                        ok = _export_drawing_to_dxf(app, doc_pdf, dxf_path)
                                    finally:
                                        if doc_pdf:
                                            try:
                                                doc_pdf.close(False)
                                            except:
                                                pass
                                    if not ok:
                                        raise RuntimeError('DXF export not supported for Drawing in this build')
                                exported['other'] += 1
                                # We consider DXF as the deliverable; skip downloading the .f2d file
                                continue
                            except Exception as ex_pdf:
                                # Fall through to download/log, but don't count as an error
                                exported['pdfFail'] += 1

                        if _DataFileDownloadHandler is None:
                            # Programmatic download not supported: record for manifest
                            try:
                                if manifest_list is not None:
                                    rel = os.path.join(rel_path, df.name) if rel_path else df.name
                                    manifest_list.append(rel)
                                exported['otherFound'] += 1
                            except:
                                pass
                        else:
                            if overwrite or not os.path.exists(out_path):
                                # Use DataFileDownloadEventHandler-based download
                                try:
                                    handler = _DataFileDownloadHandler(out_path)
                                    df.download(handler)
                                    # Give Fusion a moment to process events if needed
                                    try:
                                        adsk.doEvents()
                                    except:
                                        pass
                                    # Best-effort wait for file to appear
                                    try:
                                        import time
                                        deadline = time.time() + 5.0
                                        while time.time() < deadline and not os.path.exists(out_path) and not handler.ok:
                                            try:
                                                adsk.doEvents()
                                            except:
                                                pass
                                            time.sleep(0.05)
                                    except:
                                        pass
                                    if not os.path.exists(out_path) and not handler.ok:
                                        raise RuntimeError('Download did not produce a file')
                                except Exception as ex2:
                                    raise ex2
                            exported['other'] += 1
                    except Exception as ex:
                        exported['errors'] += 1
                        if error_list is not None:
                            try:
                                error_list.append(f"{df.name}: failed to download non-design file: {str(ex)}")
                            except:
                                pass
                else:
                    exported['skipped'] += 1
                continue

            # Open visibly to ensure active product is available in some environments
            opened_doc = app.documents.open(df, True)
            try:
                opened_doc.activate()
            except:
                pass
            # Prefer product lookup by type for robustness
            design = None
            try:
                # Prefer activeProduct after activation
                design = adsk.fusion.Design.cast(app.activeProduct)
            except:
                design = None
            if not design:
                try:
                    prod = opened_doc.products.itemByProductType('DesignProductType')
                    design = adsk.fusion.Design.cast(prod)
                except:
                    pass
            if not design:
                try:
                    design = adsk.fusion.Design.cast(opened_doc.products.item(0))
                except:
                    design = None
            if not design:
                exported['skipped'] += 1
                continue

            em = design.exportManager
            name = df.name
            lname = name.lower()
            if lname.endswith('.f3d') or lname.endswith('.f3z'):
                name = name[:name.rfind('.')]

            # Per-format export loop
            if 'stl' in fmts:
                stl_path = os.path.join(out_dir, name + '.stl')
                if overwrite or not os.path.exists(stl_path):
                    opts2 = None
                    # Try overload with filename first
                    try:
                        opts2 = em.createSTLExportOptions(design.rootComponent, stl_path)
                    except:
                        try:
                            opts2 = em.createSTLExportOptions(design.rootComponent)
                        except:
                            opts2 = None
                    # Fallback: export all solid bodies if component-based creation failed
                    if not opts2:
                        try:
                            bodies = _collect_all_brep_bodies(design.rootComponent)
                            if bodies:
                                try:
                                    opts2 = em.createSTLExportOptions(bodies, stl_path)
                                except:
                                    opts2 = em.createSTLExportOptions(bodies)
                        except:
                            pass
                    if not opts2:
                        raise RuntimeError('Failed to create STL export options')
                    try:
                        opts2.isBinaryFormat = True
                    except:
                        pass
                    try:
                        # Default mesh refinement medium if available
                        ref = adsk.fusion.MeshRefinementSettings.MeshRefinementMedium
                        opts2.meshRefinement = ref
                    except:
                        pass
                    try:
                        opts2.filename = stl_path
                    except:
                        pass
                    try:
                        adsk.doEvents()
                    except:
                        pass
                    em.execute(opts2)
                    exported['stl'] += 1
            if '3mf' in fmts:
                mf_path = os.path.join(out_dir, name + '.3mf')
                if overwrite or not os.path.exists(mf_path):
                    # Preferred API path: C3MF export (per sample script)
                    try:
                        has_c3mf = hasattr(em, 'createC3MFExportOptions')
                    except:
                        has_c3mf = False
                    if has_c3mf:
                        optsC = None
                        try:
                            optsC = em.createC3MFExportOptions(design.rootComponent, mf_path)
                        except:
                            try:
                                optsC = em.createC3MFExportOptions(design.rootComponent)
                            except:
                                optsC = None
                        if not optsC:
                            try:
                                bodies = _collect_all_brep_bodies(design.rootComponent)
                                if bodies:
                                    try:
                                        optsC = em.createC3MFExportOptions(bodies, mf_path)
                                    except:
                                        optsC = em.createC3MFExportOptions(bodies)
                            except:
                                pass
                        if optsC:
                            try:
                                optsC.filename = mf_path
                            except:
                                pass
                            try:
                                adsk.doEvents()
                            except:
                                pass
                            em.execute(optsC)
                            exported['3mf'] += 1
                            # proceed to other formats
                    opts3 = None
                    # Check API availability
                    has_3mf = hasattr(em, 'create3MFExportOptions')
                    if has_3mf:
                        try:
                            opts3 = em.create3MFExportOptions(design.rootComponent, mf_path)
                        except:
                            try:
                                opts3 = em.create3MFExportOptions(design.rootComponent)
                            except:
                                opts3 = None
                    else:
                        opts3 = None
                    # Fallback to bodies if needed
                    if has_3mf and not opts3:
                        try:
                            bodies = _collect_all_brep_bodies(design.rootComponent)
                            if bodies:
                                try:
                                    opts3 = em.create3MFExportOptions(bodies, mf_path)
                                except:
                                    opts3 = em.create3MFExportOptions(bodies)
                        except:
                            pass
                    # Alternate path: MeshExportOptions if available
                    if not opts3 and hasattr(em, 'createMeshExportOptions'):
                        mesh_opts = None
                        try:
                            # Try explicit 3-arg overload specifying 3MF format if available
                            try:
                                mesh_opts = em.createMeshExportOptions(
                                    design.rootComponent,
                                    mf_path,
                                    adsk.fusion.MeshFileFormat.MeshFileFormat3MF
                                )
                            except:
                                mesh_opts = em.createMeshExportOptions(design.rootComponent, mf_path)
                        except:
                            try:
                                bodies = _collect_all_brep_bodies(design.rootComponent)
                                if bodies:
                                    try:
                                        mesh_opts = em.createMeshExportOptions(
                                            bodies,
                                            mf_path,
                                            adsk.fusion.MeshFileFormat.MeshFileFormat3MF
                                        )
                                    except:
                                        try:
                                            mesh_opts = em.createMeshExportOptions(bodies, mf_path)
                                        except:
                                            mesh_opts = em.createMeshExportOptions(bodies)
                            except:
                                pass
                        if mesh_opts:
                            # Try to set file format to 3MF across possible property names
                            set_ok = False
                            for prop in ('fileFormat', 'meshFileFormat', 'format'):
                                try:
                                    setattr(mesh_opts, prop, adsk.fusion.MeshFileFormat.MeshFileFormat3MF)
                                    set_ok = True
                                    break
                                except:
                                    pass
                            # Mesh refinement if exposed
                            try:
                                mesh_opts.meshRefinement = adsk.fusion.MeshRefinementSettings.MeshRefinementMedium
                            except:
                                pass
                            if set_ok:
                                try:
                                    adsk.doEvents()
                                except:
                                    pass
                                em.execute(mesh_opts)
                                exported['3mf'] += 1
                                opts3 = 'done-mesh'  # mark as done
                    if not has_3mf or not opts3:
                        # Graceful fallback to STL when 3MF isn't available or options fail
                        try:
                            stl_path = os.path.join(out_dir, name + '.stl')
                            opts2 = None
                            try:
                                opts2 = em.createSTLExportOptions(design.rootComponent, stl_path)
                            except:
                                try:
                                    opts2 = em.createSTLExportOptions(design.rootComponent)
                                except:
                                    opts2 = None
                            if not opts2:
                                bodies = _collect_all_brep_bodies(design.rootComponent)
                                if bodies:
                                    try:
                                        opts2 = em.createSTLExportOptions(bodies, stl_path)
                                    except:
                                        opts2 = em.createSTLExportOptions(bodies)
                            if not opts2:
                                raise RuntimeError('Failed to create 3MF export options and STL fallback options')
                            try:
                                opts2.isBinaryFormat = True
                            except:
                                pass
                            try:
                                ref = adsk.fusion.MeshRefinementSettings.MeshRefinementMedium
                                opts2.meshRefinement = ref
                            except:
                                pass
                            try:
                                opts2.filename = stl_path
                            except:
                                pass
                            try:
                                adsk.doEvents()
                            except:
                                pass
                            em.execute(opts2)
                            exported['stl'] += 1
                        except:
                            raise RuntimeError('Failed to create 3MF export options')
                    else:
                        if opts3 != 'done-mesh':
                            try:
                                opts3.filename = mf_path
                            except:
                                pass
                            try:
                                adsk.doEvents()
                            except:
                                pass
                            em.execute(opts3)
                            exported['3mf'] += 1
            if 'obj' in fmts:
                obj_path = os.path.join(out_dir, name + '.obj')
                if overwrite or not os.path.exists(obj_path):
                    optsO = None
                    try:
                        optsO = em.createOBJExportOptions(design.rootComponent, obj_path)
                    except:
                        try:
                            optsO = em.createOBJExportOptions(design.rootComponent)
                        except:
                            optsO = None
                    if not optsO:
                        raise RuntimeError('Failed to create OBJ export options')
                    try:
                        optsO.filename = obj_path
                    except:
                        pass
                    try:
                        adsk.doEvents()
                    except:
                        pass
                    em.execute(optsO)
                    exported['obj'] += 1

            # DXF (flat pattern) export if requested
            if 'dxf' in fmts:
                try:
                    # Try to get a flat pattern product from the opened document
                    flat_prod = None
                    try:
                        flat_prod = opened_doc.products.itemByProductType('FlatPatternProductType')
                    except:
                        flat_prod = None
                    if flat_prod:
                        flat = None
                        try:
                            flat = flat_prod.flatPattern
                        except:
                            flat = None
                        if flat:
                            dxf_path = os.path.join(out_dir, name + '.dxf')
                            if overwrite or not os.path.exists(dxf_path):
                                try:
                                    expMgr = getattr(flat_prod, 'exportManager', None)
                                    if expMgr and hasattr(expMgr, 'createDXFFlatPatternExportOptions'):
                                        fp_opts = expMgr.createDXFFlatPatternExportOptions(dxf_path, flat)
                                        ok = expMgr.execute(fp_opts)
                                        if ok:
                                            exported['other'] += 1
                                    # If execute returned False, treat as non-fatal and continue
                                except Exception as ex_dxf:
                                    # Non-fatal: record as error detail but keep going
                                    try:
                                        if error_list is not None:
                                            error_list.append(f"{df.name}: flat pattern DXF export failed: {str(ex_dxf)}")
                                        exported['errors'] += 1
                                    except:
                                        pass
                except Exception:
                    # Non-fatal outer protection for DXF branch
                    pass

            exported['designs'] += 1

        except Exception as ex:
            exported['errors'] += 1
            try:
                if error_list is not None:
                    error_list.append(f"{df.name}: {str(ex)}")
            except:
                pass
        finally:
            if opened_doc:
                try:
                    opened_doc.close(False)
                except:
                    pass

    for i in range(folder.dataFolders.count):
        sub = folder.dataFolders.item(i)
        sub_rel = os.path.join(rel_path, sub.name) if rel_path else sub.name
        stats = traverse_and_export(app, ui, sub, base_output, fmts, overwrite, sub_rel, error_list, include_other_files, other_exts, manifest_list, export_drawing_dxf)
        for k in exported:
            exported[k] += stats.get(k, 0)

    return exported

# UI + dialog

_opts_ready = False
_opts = {}
_handlers = []  # Keep event handlers alive
_isUpdatingUI = False  # Re-entrancy guard for UI updates
_drawing_pdf_not_supported = False  # cache to avoid repeated PDF attempts on unsupported builds

class CmdCreated(adsk.core.CommandCreatedEventHandler):
    def __init__(self): super().__init__()
    def notify(self, args):
        try:
            cmd = adsk.core.Command.cast(args.command)
            onExec = CmdExecute()
            onChanged = CmdInputChanged()
            cmd.execute.add(onExec)
            cmd.inputChanged.add(onChanged)
            _handlers.extend([onExec, onChanged])
            inputs = cmd.commandInputs

            # Local output directory
            inputs.addStringValueInput('outDir', 'Local output folder', '')
            inputs.addBoolValueInput('pickOut', 'Choose Folder…', False, '', True)

            # Hub dropdown (to ensure correct project list)
            ddHub = inputs.addDropDownCommandInput('hubDD', 'Hub', adsk.core.DropDownStyles.TextListDropDownStyle)
            data = _app.data
            hubs = list_hubs(data)
            active_hub_name = data.activeHub.name if data.activeHub else None
            if hubs:
                for h in hubs:
                    ddHub.listItems.add(h.name, h.name == active_hub_name)
            else:
                ddHub.listItems.add('(No hubs found)', True)

            # Project dropdown
            ddProj = inputs.addDropDownCommandInput('projDD', 'Project', adsk.core.DropDownStyles.TextListDropDownStyle)
            # Initial population (we'll override selection from prefs below)
            projects = [p for p in list_projects(data) if not getattr(p, 'isArchived', False)]
            if projects:
                first = True
                for p in projects:
                    ddProj.listItems.add(p.name, first)
                    first = False
            else:
                ddProj.listItems.add('(No projects found)', True)

            # Folder path (string) + helper button to show available paths
            inputs.addStringValueInput('folderPath', 'Fusion folder path', '(Project root)')
            inputs.addBoolValueInput('showPaths', 'Show Folder Paths…', False, '', True)

            # Export formats (multi-select)
            inputs.addBoolValueInput('fmt3mf', 'Export 3MF', True, '', True)
            inputs.addBoolValueInput('fmtstl', 'Export STL', True, '', False)
            inputs.addBoolValueInput('fmtobj', 'Export OBJ', True, '', False)
            inputs.addBoolValueInput('fmtdxf', 'Export DXF (flat pattern)', True, '', False)
            inputs.addBoolValueInput('includeOther', 'Also download other project files (e.g., DXF/DWG/PDF/images)', True, '', False)
            inputs.addStringValueInput('otherExts', 'Other file extensions (comma-separated)', 'f2d,dxf,dwg,pdf,svg,png,jpg')
            inputs.addBoolValueInput('otherManifest', 'If direct download isn’t supported, list them in a log.txt', True, '', True)
            inputs.addBoolValueInput('exportDrawingDxf', 'Export Fusion Drawings (f2d) to DXF', True, '', True)

            # Selection summary
            inputs.addTextBoxCommandInput('summary', 'Summary', 'Enter a folder path or use "Show Folder Paths…". Use (Project root) for top level.', 6, True)
            # Prefer selecting an 'Admin' project by default when present
            try:
                prefer_index = -1
                for i in range(ddProj.listItems.count):
                    name = ddProj.listItems.item(i).name or ''
                    if 'admin' in name.lower():
                        prefer_index = i
                        break
                if prefer_index >= 0:
                    for i in range(ddProj.listItems.count):
                        ddProj.listItems.item(i).isSelected = (i == prefer_index)
            except:
                pass

        except:
            _ui.messageBox('CmdCreated error:\n' + traceback.format_exc())

class CmdInputChanged(adsk.core.InputChangedEventHandler):
    def __init__(self): super().__init__()
    def notify(self, args):
        try:
            eventArgs = adsk.core.InputChangedEventArgs.cast(args)
            changedInput = eventArgs.input
            
            if changedInput.id == 'pickOut':
                pickOutInput = adsk.core.BoolValueCommandInput.cast(changedInput)
                if pickOutInput.value:
                    # Open folder picker dialog
                    folderDialog = _ui.createFolderDialog()
                    folderDialog.title = "Select Output Folder for 3D Models"
                    # If no OutDir yet, start from a sensible root so the dialog opens correctly
                    try:
                        outDirInput = adsk.core.StringValueCommandInput.cast(eventArgs.inputs.itemById('outDir'))
                        curr_val = outDirInput.value.strip() if outDirInput and outDirInput.value else ''
                        init_dir = curr_val if curr_val and os.path.isdir(curr_val) else _default_initial_dir()
                        try:
                            folderDialog.initialDirectory = init_dir
                        except:
                            pass
                    except:
                        pass
                    dialogResult = folderDialog.showDialog()
                    
                    if dialogResult == adsk.core.DialogResults.DialogOK:
                        outDirInput = adsk.core.StringValueCommandInput.cast(eventArgs.inputs.itemById('outDir'))
                        try:
                            # Use exactly the folder returned by the dialog and normalize it
                            outDirInput.value = os.path.normpath(folderDialog.folder)
                        except:
                            outDirInput.value = folderDialog.folder
                    
                    # Reset the button
                    pickOutInput.value = False

            # When hub changes, switch active hub and repopulate projects
            if changedInput.id == 'hubDD':
                hubDD = adsk.core.DropDownCommandInput.cast(changedInput)
                selected_hub = None
                for it in hubDD.listItems:
                    if it.isSelected:
                        selected_hub = it.name
                        break
                data = _app.data
                # Set active hub
                if selected_hub and selected_hub != '(No hubs found)':
                    for h in list_hubs(data):
                        if h.name == selected_hub:
                            try:
                                data.activeHub = h
                            except:
                                pass
                            break
                # Rebuild projects list
                projDD = adsk.core.DropDownCommandInput.cast(eventArgs.inputs.itemById('projDD'))
                try:
                    while projDD.listItems.count > 0:
                        projDD.listItems.item(0).deleteMe()
                except:
                    pass
                projects = list_projects(data)
                if projects:
                    first = True
                    for p in projects:
                        projDD.listItems.add(p.name, first)
                        first = False
                else:
                    projDD.listItems.add('(No projects found)', True)
                # Trigger project-change branch below
                changedInput = projDD

            # When project changes, reset folder path string and summary
            if changedInput.id == 'projDD':
                projDD = adsk.core.DropDownCommandInput.cast(changedInput)
                selected = None
                for it in projDD.listItems:
                    if it.isSelected:
                        selected = it.name
                        break
                if selected:
                    data = _app.data
                    # find project by name
                    project = None
                    for p in list_projects(data):
                        if p.name == selected:
                            project = p
                            break
                    # Reset current path to root
                    if project:
                        try:
                            fp = adsk.core.StringValueCommandInput.cast(eventArgs.inputs.itemById('folderPath'))
                            if fp:
                                fp.value = '(Project root)'
                        except:
                            pass

                    # Update summary (use plain text for safety)
                    try:
                        summary = adsk.core.TextBoxCommandInput.cast(eventArgs.inputs.itemById('summary'))
                        summary.text = 'Project selected: {}. Enter a folder path or click "Show Folder Paths…"'.format(selected)
                    except:
                        pass

            # Show folder paths helper: list a few available paths to copy
            if changedInput.id == 'showPaths':
                btn = adsk.core.BoolValueCommandInput.cast(changedInput)
                if btn and btn.value:
                    btn.value = False
                    # Determine selected project
                    projDD = adsk.core.DropDownCommandInput.cast(eventArgs.inputs.itemById('projDD'))
                    proj_name = None
                    for it in projDD.listItems:
                        if it.isSelected:
                            proj_name = it.name
                            break
                    if not proj_name or proj_name == '(No projects found)':
                        _ui.messageBox('Select a project first to list its folders.')
                        return
                    data = _app.data
                    project = None
                    for p in list_projects(data):
                        if p.name == proj_name:
                            project = p
                            break
                    if not project:
                        _ui.messageBox('Project not found.')
                        return
                    # Build a limited list of folder paths to keep it light
                    paths = ['(Project root)']
                    try:
                        root = project.rootFolder
                        # Only show first-level and second-level to avoid heavy traversal
                        for i in range(root.dataFolders.count):
                            f1 = root.dataFolders.item(i)
                            paths.append(f1.name)
                            # up to N children per folder to prevent overload
                            n = min(20, f1.dataFolders.count)
                            for j in range(n):
                                f2 = f1.dataFolders.item(j)
                                paths.append(f1.name + '/' + f2.name)
                    except:
                        pass
                    # Show the list in a simple message (multiple pages if long)
                    if not paths:
                        _ui.messageBox('No folders found in this project.')
                    else:
                        # Chunk into blocks to avoid dialog limits
                        chunk = []
                        count = 0
                        for pth in paths:
                            chunk.append(pth)
                            count += 1
                            if len(chunk) >= 40:
                                _ui.messageBox('\n'.join(chunk))
                                chunk = []
                        if chunk:
                            _ui.messageBox('\n'.join(chunk))
        except:
            _ui.messageBox('CmdInputChanged error:\n' + traceback.format_exc())

class CmdExecute(adsk.core.CommandEventHandler):
    def __init__(self): super().__init__()
    def notify(self, args):
        global _opts_ready, _opts
        try:
            inputs = adsk.core.CommandEventArgs.cast(args).command.commandInputs
            # Read dropdown selections
            hubDD = adsk.core.DropDownCommandInput.cast(inputs.itemById('hubDD'))
            projDD = adsk.core.DropDownCommandInput.cast(inputs.itemById('projDD'))
            folderPathInput = adsk.core.StringValueCommandInput.cast(inputs.itemById('folderPath'))
            fmt3 = adsk.core.BoolValueCommandInput.cast(inputs.itemById('fmt3mf'))
            fmtS = adsk.core.BoolValueCommandInput.cast(inputs.itemById('fmtstl'))
            fmtO = adsk.core.BoolValueCommandInput.cast(inputs.itemById('fmtobj'))
            fmtD = adsk.core.BoolValueCommandInput.cast(inputs.itemById('fmtdxf'))
            inclOther = adsk.core.BoolValueCommandInput.cast(inputs.itemById('includeOther'))
            otherExtsInput = adsk.core.StringValueCommandInput.cast(inputs.itemById('otherExts'))
            otherManifestInput = adsk.core.BoolValueCommandInput.cast(inputs.itemById('otherManifest'))
            exportDrawingDxfInput = adsk.core.BoolValueCommandInput.cast(inputs.itemById('exportDrawingDxf'))
            out_dir = adsk.core.StringValueCommandInput.cast(inputs.itemById('outDir')).value.strip()

            # Extract values
            proj = None
            for it in projDD.listItems:
                if it.isSelected:
                    proj = it.name
                    break
            # Use typed folder path
            folder_path = folderPathInput.value.strip() if folderPathInput else ''
            selected_formats = []
            if fmt3 and fmt3.value:
                selected_formats.append('3mf')
            if fmtS and fmtS.value:
                selected_formats.append('stl')
            if fmtO and fmtO.value:
                selected_formats.append('obj')
            if fmtD and fmtD.value:
                selected_formats.append('dxf')

            if not proj or proj == '(No projects found)':
                _ui.messageBox('Please select a project.')
                return
            if not out_dir:
                _ui.messageBox('Please choose an output folder where your 3D models will be saved.')
                return
            if not selected_formats:
                _ui.messageBox('Please select at least one export format.')
                return

            # resolve hub then project
            data = _app.data
            # Set active hub based on selection (if provided)
            sel_hub = None
            if hubDD:
                for it in hubDD.listItems:
                    if it.isSelected:
                        sel_hub = it.name
                        break
                if sel_hub and sel_hub != '(No hubs found)':
                    for h in list_hubs(data):
                        if h.name == sel_hub:
                            try:
                                data.activeHub = h
                            except:
                                pass
                            break

            prjs = list_projects(data)
            project = None
            for p in prjs:
                if p.name == proj:
                    project = p
                    break
            if not project:
                _ui.messageBox(f"Project not found: '{proj}'")
                return

            # now try to resolve the folder
            # '(Project root)' means rootFolder
            if not folder_path or folder_path == '(Project root)':
                folder = project.rootFolder
            else:
                folder = find_folder_by_path(project, folder_path)
            if not folder:
                _ui.messageBox(f"Folder not found: '{folder_path}'")
                return

            ensure_dir(out_dir)

            error_list = []
            manifest = [] if (inclOther and inclOther.value and otherManifestInput and otherManifestInput.value) else None
            exts = None
            try:
                raw = otherExtsInput.value if otherExtsInput else ''
                exts = [s.strip() for s in raw.split(',') if s.strip()]
            except:
                exts = None
            stats = traverse_and_export(
                _app,
                _ui,
                folder,
                out_dir,
                selected_formats,
                True,
                '',
                error_list,
                include_other_files=(inclOther.value if inclOther else False),
                other_exts=exts,
                manifest_list=manifest,
                export_drawing_dxf=(exportDrawingDxfInput.value if exportDrawingDxfInput else False)
            )

            msg = (
                f"Done.\nSTL: {stats['stl']} | 3MF: {stats['3mf']} | OBJ: {stats['obj']} | Other files: {stats.get('other',0)}\n"
                f"Errors: {stats['errors']}\nDesigns processed: {stats['designs']}\nSkipped: {stats['skipped']}"
            )
            if stats['errors'] > 0 and error_list:
                # Show up to first 8 error lines for quick diagnosis
                preview = "\n".join(error_list[:8])
                msg += f"\n\nFirst errors:\n{preview}"
            # Hint user if 3MF requested but STL fallback happened
            if ('3mf' in selected_formats) and stats['3mf'] == 0 and stats['stl'] > 0:
                msg += "\n\nNote: 3MF export wasn't available for some designs; exported STL instead."
            # Note if DXF export for drawings isn't supported
            if stats.get('pdfFail', 0) > 0:
                msg += "\n\nNote: Drawing-to-DXF export might not be supported in this Fusion build. Drawing files were added to log.txt."
            # If we captured a manifest list (because direct download isn’t supported), write it out
            try:
                if manifest is not None and len(manifest) > 0:
                    manifest_path = os.path.join(out_dir, 'log.txt')
                    with open(manifest_path, 'w', encoding='utf-8') as f:
                        f.write('# Export log\n')
                        f.write('# DXF export for Drawings: {}\n'.format('unsupported' if stats.get('pdfFail',0)>0 else 'attempted'))
                        f.write('# The following files are present in Fusion but were not downloaded automatically:\n')
                        for rel in manifest:
                            f.write(rel + '\n')
                    msg += f"\n\nOther files not downloaded automatically were listed in: {manifest_path}"
            except:
                pass

            _ui.messageBox(msg)

            _opts_ready = True

        except:
            _ui.messageBox('CmdExecute error:\n' + traceback.format_exc())

def run(context):
    global _app, _ui, _opts_ready, _opts
    _opts_ready = False
    _opts = {}
    try:
        _app = adsk.core.Application.get()
        _ui = _app.userInterface
        # Ensure we don't re-create an existing command definition with the same ID
        existing = _ui.commandDefinitions.itemById('Folder3DExport')
        if existing:
            try:
                existing.deleteMe()
            except:
                pass
        cmdDef = _ui.commandDefinitions.addButtonDefinition('Folder3DExport', 'Export Fusion Folder (3D Print)', 'Pick output directory, choose Fusion project and folder, then export as 3MF/STL/OBJ')
        oncr = CmdCreated()
        cmdDef.commandCreated.add(oncr)
        cmd = cmdDef.execute()

        # block loop like your original
        while not _opts_ready and _app:
            adsk.doEvents()

        try:
            cmdDef.deleteMe()
        except:
            pass

    except:
        if _ui:
            _ui.messageBox('run error:\n' + traceback.format_exc())

def stop(context):
    pass
