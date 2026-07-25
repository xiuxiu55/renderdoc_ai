"""A Model Context Protocol (MCP) server exposing RenderDoc replay analysis.

This server lets an AI agent load a RenderDoc capture (``.rdc``) and inspect it:
walk the action/draw tree, move the replay cursor, read pipeline state, dump
shader disassembly and reflection, list resources/textures, save textures to
disk and sample GPU counters.

Run it over stdio (the default MCP transport):

    python -m renderdoc_mcp.server

The ``renderdoc`` native module must be importable. Set RENDERDOC_MODULE_PATH to
the directory containing ``renderdoc.pyd`` / ``renderdoc.dll`` (or pass
``module_path`` to ``load_capture``). The Python running this server must match
the Python version RenderDoc was built against.
"""
# crsr_8280c549b12556e65e97da064a49cd3286c5282854390f0635e3b21b96db307b
from __future__ import annotations

import json
from typing import Any, Optional

try:
    from mcp.server.fastmcp import FastMCP
except ImportError as exc:  # pragma: no cover - surfaced at startup
    raise SystemExit(
        "The 'mcp' package is required. Install it with: pip install \"mcp[cli]\"\n"
        f"Original error: {exc}"
    )

# Support both `python -m renderdoc_mcp.server` and `python server.py`.
try:  # pragma: no cover
    from renderdoc_mcp import rd_session as _session_mod  # type: ignore
    from renderdoc_mcp.rd_session import Session, RenderDocError  # type: ignore
except ImportError:  # pragma: no cover
    import rd_session as _session_mod  # type: ignore
    from rd_session import Session, RenderDocError  # type: ignore


mcp = FastMCP("renderdoc")
session = Session()


def _rd():
    """Return the imported renderdoc module, or raise if not loaded yet."""
    module = _session_mod.rd
    if module is None:
        raise RenderDocError("No capture is loaded. Call load_capture first.")
    return module


# deep inspect tools registered after helpers below
_DEEP_TOOLS_REGISTERED = False


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------


def _rid(resid: Any) -> Optional[str]:
    if resid is None:
        return None
    try:
        if int(resid) == 0:
            return None
    except (TypeError, ValueError):
        pass
    return str(resid)


def _enum(value: Any) -> str:
    return str(value)


def _resource_name_map() -> dict:
    controller = session.require_controller()
    names: dict = {}
    for res in controller.GetResources():
        names[str(res.resourceId)] = res.name
    return names


def _action_name(action: Any) -> str:
    controller = session.require_controller()
    try:
        return action.GetName(controller.GetStructuredFile())
    except Exception:  # noqa: BLE001
        return getattr(action, "customName", "") or ""


def _action_summary(action: Any, names: Optional[dict] = None) -> dict:
    data: dict = {
        "eventId": int(action.eventId),
        "actionId": int(getattr(action, "actionId", 0)),
        "name": _action_name(action),
        "flags": _enum(action.flags),
    }
    flags = action.flags
    module = _session_mod.rd
    if module is not None:
        if flags & module.ActionFlags.Drawcall:
            data["numIndices"] = int(action.numIndices)
            data["numInstances"] = int(action.numInstances)
            data["indexOffset"] = int(action.indexOffset)
            data["baseVertex"] = int(action.baseVertex)
            data["vertexOffset"] = int(action.vertexOffset)
            data["instanceOffset"] = int(action.instanceOffset)
        if flags & module.ActionFlags.Dispatch:
            data["dispatchDimension"] = [int(x) for x in action.dispatchDimension]
    outputs = [_rid(o) for o in getattr(action, "outputs", []) if _rid(o)]
    if outputs:
        data["outputs"] = outputs
    depth = _rid(getattr(action, "depthOut", None))
    if depth:
        data["depthOutput"] = depth
    return data


def _ensure_deep_tools() -> None:
    global _DEEP_TOOLS_REGISTERED
    if _DEEP_TOOLS_REGISTERED:
        return
    try:
        from renderdoc_mcp.deep_inspect import register_deep_tools  # type: ignore
    except ImportError:
        from deep_inspect import register_deep_tools  # type: ignore
    register_deep_tools(
        mcp, session, RenderDocError, _rd, _rid, _enum, _resource_name_map
    )
    _DEEP_TOOLS_REGISTERED = True


_ensure_deep_tools()


# ---------------------------------------------------------------------------
# Session lifecycle tools
# ---------------------------------------------------------------------------


@mcp.tool()
def load_capture(path: str, module_path: Optional[str] = None) -> str:
    """Load a RenderDoc capture (.rdc) and begin replay analysis.

    Args:
        path: Absolute path to the .rdc capture file.
        module_path: Optional directory containing the renderdoc python module
            and native library. Overrides RENDERDOC_MODULE_PATH.

    Returns a JSON summary with the detected API, renderer and action counts.
    """
    info = session.load(path, module_path)
    return json.dumps(info, indent=2)


@mcp.tool()
def close_capture() -> str:
    """Close the currently loaded capture and release replay resources."""
    closed = session.close()
    return json.dumps({"closed": closed})


@mcp.tool()
def get_status() -> str:
    """Report whether a capture is loaded and the current replay cursor event."""
    status = {
        "loaded": session.loaded,
        "filename": session.filename,
        "currentEvent": session.current_event,
        "totalActions": len(session.all_actions()) if session.loaded else 0,
    }
    return json.dumps(status, indent=2)


@mcp.tool()
def get_capture_info() -> str:
    """Return details about the loaded capture: API, driver, frame info."""
    controller = session.require_controller()

    def _info() -> dict:
        props = controller.GetAPIProperties()
        result: dict = {
            "filename": session.filename,
            "api": _enum(props.pipelineType),
            "localRenderer": _enum(props.localRenderer),
            "vendor": _enum(getattr(props, "vendor", "")),
            "degraded": bool(getattr(props, "degraded", False)),
            "shaderDebugging": bool(getattr(props, "shaderDebugging", False)),
            "pixelHistory": bool(getattr(props, "pixelHistory", False)),
            "rootActionCount": len(controller.GetRootActions()),
            "totalActions": len(session.all_actions()),
        }
        try:
            fi = controller.GetFrameInfo()
            result["frame"] = {
                "frameNumber": int(fi.frameNumber),
                "captureTime": int(getattr(fi, "captureTime", 0)),
                "uncompressedFileSize": int(getattr(fi, "uncompressedFileSize", 0)),
            }
        except Exception:  # noqa: BLE001
            pass
        return result

    return json.dumps(session.run(_info), indent=2)


# ---------------------------------------------------------------------------
# Action / event tools
# ---------------------------------------------------------------------------


@mcp.tool()
def list_actions(
    parent_event_id: Optional[int] = None,
    max_depth: int = 0,
    drawcalls_only: bool = False,
) -> str:
    """List actions (draws, dispatches, clears, marker regions) as a tree.

    Args:
        parent_event_id: If given, only list children under this marker region.
            Otherwise list from the root.
        max_depth: Maximum nesting depth to include (0 = unlimited).
        drawcalls_only: If True, only include actual draw/dispatch actions.
    """
    controller = session.require_controller()

    def _list() -> list:
        names = _resource_name_map()
        module = _session_mod.rd

        if parent_event_id is not None:
            root = session.action_for_event(parent_event_id)
            top = list(root.children)
        else:
            top = list(controller.GetRootActions())

        def is_draw(a: Any) -> bool:
            return bool(
                a.flags & (module.ActionFlags.Drawcall | module.ActionFlags.Dispatch)
            )

        def walk(actions: list, depth: int) -> list:
            out = []
            for a in actions:
                include = (not drawcalls_only) or is_draw(a)
                node = _action_summary(a, names) if include else None
                children: list = []
                if a.children and (max_depth == 0 or depth < max_depth):
                    children = walk(list(a.children), depth + 1)
                if node is not None:
                    if children:
                        node["children"] = children
                    out.append(node)
                else:
                    out.extend(children)
            return out

        return walk(top, 1)

    return json.dumps(session.run(_list), indent=2)


@mcp.tool()
def get_action(event_id: int) -> str:
    """Return detailed information about a single action by its eventId.

    Includes the draw parameters, output targets, and the list of API events
    (with their API call names) that make up this action.
    """
    controller = session.require_controller()

    def _get() -> dict:
        action = session.action_for_event(event_id)
        names = _resource_name_map()
        data = _action_summary(action, names)

        sdfile = controller.GetStructuredFile()
        events = []
        for ev in action.events:
            entry = {"eventId": int(ev.eventId), "chunkIndex": int(ev.chunkIndex)}
            try:
                chunk = sdfile.chunks[ev.chunkIndex]
                entry["apiCall"] = chunk.name
            except Exception:  # noqa: BLE001
                pass
            events.append(entry)
        data["events"] = events

        parent = getattr(action, "parent", None)
        if parent is not None and int(getattr(parent, "eventId", 0)) != 0:
            data["parentEventId"] = int(parent.eventId)
        return data

    return json.dumps(session.run(_get), indent=2)


@mcp.tool()
def set_event(event_id: int, force: bool = False) -> str:
    """Move the replay cursor to the given eventId.

    All subsequent state/texture/shader queries reflect the state immediately
    after this event has executed.
    """
    session.set_event(event_id, force)
    return json.dumps({"currentEvent": session.current_event})


@mcp.tool()
def get_event_chunk(event_id: int) -> str:
    """Return the structured API call (chunk) details for an event.

    This exposes the exact API function name and its parameter values as they
    were recorded in the capture.
    """
    controller = session.require_controller()

    def _get() -> dict:
        action = session.action_for_event(event_id)
        sdfile = controller.GetStructuredFile()

        target_chunk = None
        for ev in action.events:
            if int(ev.eventId) == int(event_id):
                target_chunk = sdfile.chunks[ev.chunkIndex]
                break
        if target_chunk is None and action.events:
            target_chunk = sdfile.chunks[action.events[-1].chunkIndex]
        if target_chunk is None:
            raise RenderDocError(f"No chunk found for eventId {event_id}.")

        def serialise_obj(obj: Any, depth: int = 0) -> Any:
            if depth > 6:
                return "<max depth>"
            children = list(getattr(obj, "data", obj).children) if hasattr(
                getattr(obj, "data", obj), "children"
            ) else []
            if children:
                return {c.name: serialise_obj(c, depth + 1) for c in children}
            try:
                val = obj.AsString() if hasattr(obj, "AsString") else str(obj)
            except Exception:  # noqa: BLE001
                val = str(obj)
            return val

        params = {}
        for child in target_chunk.data.children:
            params[child.name] = serialise_obj(child)

        return {
            "eventId": int(event_id),
            "apiCall": target_chunk.name,
            "parameters": params,
        }

    return json.dumps(session.run(_get), indent=2, default=str)


# ---------------------------------------------------------------------------
# Pipeline state tools
# ---------------------------------------------------------------------------

_STAGES = ["Vertex", "Hull", "Domain", "Geometry", "Pixel", "Compute"]


def _stage_enum(name: str) -> Any:
    module = _rd()
    try:
        return getattr(module.ShaderStage, name)
    except AttributeError as exc:
        raise RenderDocError(
            f"Unknown shader stage '{name}'. Valid: {', '.join(_STAGES)}"
        ) from exc


@mcp.tool()
def get_pipeline_state(event_id: Optional[int] = None) -> str:
    """Return an API-agnostic summary of the bound pipeline state.

    Includes primitive topology, bound shaders per stage, viewports, color
    render targets, depth target and blend state. If ``event_id`` is provided
    the cursor is moved there first.
    """
    if event_id is not None:
        session.set_event(event_id)
    controller = session.require_controller()

    def _get() -> dict:
        state = controller.GetPipelineState()
        names = _resource_name_map()

        def named(resid: Any) -> Optional[dict]:
            rid = _rid(resid)
            if rid is None:
                return None
            return {"id": rid, "name": names.get(rid, "")}

        result: dict = {
            "eventId": session.current_event,
            "topology": _enum(state.GetPrimitiveTopology()),
        }

        stages = {}
        for name in _STAGES:
            stage = _stage_enum(name)
            shader = state.GetShader(stage)
            info = named(shader)
            if info is None:
                continue
            info["entryPoint"] = state.GetShaderEntryPoint(stage)
            stages[name] = info
        result["shaders"] = stages

        try:
            viewports = []
            for i in range(8):
                vp = state.GetViewport(i)
                if vp.width == 0 and vp.height == 0:
                    continue
                viewports.append(
                    {
                        "x": vp.x,
                        "y": vp.y,
                        "width": vp.width,
                        "height": vp.height,
                        "minDepth": vp.minDepth,
                        "maxDepth": vp.maxDepth,
                    }
                )
            if viewports:
                result["viewports"] = viewports
        except Exception:  # noqa: BLE001
            pass

        try:
            targets = []
            for t in state.GetOutputTargets():
                info = named(t.resource)
                if info:
                    targets.append(info)
            result["colorTargets"] = targets
        except Exception:  # noqa: BLE001
            pass

        try:
            depth = named(state.GetDepthTarget().resource)
            if depth:
                result["depthTarget"] = depth
        except Exception:  # noqa: BLE001
            pass

        return result

    return json.dumps(session.run(_get), indent=2)


# ---------------------------------------------------------------------------
# Resource / texture / buffer tools
# ---------------------------------------------------------------------------


@mcp.tool()
def list_resources(name_filter: Optional[str] = None) -> str:
    """List all resources in the capture, optionally filtered by name substring."""
    controller = session.require_controller()

    def _list() -> list:
        out = []
        for res in controller.GetResources():
            name = res.name
            if name_filter and name_filter.lower() not in name.lower():
                continue
            out.append(
                {
                    "id": str(res.resourceId),
                    "name": name,
                    "type": _enum(res.type),
                }
            )
        return out

    return json.dumps(session.run(_list), indent=2)


@mcp.tool()
def list_textures(name_filter: Optional[str] = None) -> str:
    """List all textures with dimensions, format, mip and array counts."""
    controller = session.require_controller()

    def _list() -> list:
        names = _resource_name_map()
        out = []
        for tex in controller.GetTextures():
            rid = str(tex.resourceId)
            name = names.get(rid, "")
            if name_filter and name_filter.lower() not in name.lower():
                continue
            out.append(
                {
                    "id": rid,
                    "name": name,
                    "width": int(tex.width),
                    "height": int(tex.height),
                    "depth": int(tex.depth),
                    "arraySize": int(tex.arraysize),
                    "mips": int(tex.mips),
                    "msSamp": int(getattr(tex, "msSamp", 1)),
                    "format": _enum(tex.format.Name()) if hasattr(tex.format, "Name") else _enum(tex.format),
                    "type": _enum(tex.type),
                }
            )
        return out

    return json.dumps(session.run(_list), indent=2)


@mcp.tool()
def list_buffers() -> str:
    """List all buffers in the capture with their sizes."""
    controller = session.require_controller()

    def _list() -> list:
        names = _resource_name_map()
        out = []
        for buf in controller.GetBuffers():
            rid = str(buf.resourceId)
            out.append(
                {
                    "id": rid,
                    "name": names.get(rid, ""),
                    "length": int(buf.length),
                    "flags": _enum(buf.creationFlags),
                }
            )
        return out

    return json.dumps(session.run(_list), indent=2)


@mcp.tool()
def save_texture(
    resource_id: str,
    out_path: str,
    event_id: Optional[int] = None,
    file_type: str = "PNG",
    mip: int = 0,
    array_slice: int = 0,
) -> str:
    """Save a texture's contents to an image file on disk.

    Args:
        resource_id: The texture id from list_textures. Accepts either the full
            form ("ResourceId::47") or just the number ("47").
        out_path: Destination file path.
        event_id: Optional event to move to before capturing the texture.
        file_type: One of PNG, JPG, BMP, TGA, HDR, DDS.
        mip: Mip level to save.
        array_slice: Array slice / cubemap face to save.
    """
    if event_id is not None:
        session.set_event(event_id)
    controller = session.require_controller()
    module = _rd()

    wanted = str(resource_id)
    if not wanted.startswith("ResourceId::"):
        wanted = f"ResourceId::{wanted}"

    def _save() -> dict:
        try:
            ftype = getattr(module.FileType, file_type.upper())
        except AttributeError as exc:
            raise RenderDocError(
                f"Unknown file_type '{file_type}'. Valid: PNG, JPG, BMP, TGA, HDR, DDS"
            ) from exc

        resid = None
        for tex in controller.GetTextures():
            if str(tex.resourceId) == wanted:
                resid = tex.resourceId
                break
        if resid is None:
            raise RenderDocError(f"Texture with id '{resource_id}' not found.")

        texsave = module.TextureSave()
        texsave.resourceId = resid
        texsave.mip = int(mip)
        texsave.slice.sliceIndex = int(array_slice)
        texsave.destType = ftype
        if ftype in (module.FileType.JPG, module.FileType.HDR, module.FileType.BMP):
            texsave.alpha = module.AlphaMapping.BlendToCheckerboard

        result = controller.SaveTexture(texsave, out_path)
        if result != module.ResultCode.Succeeded:
            raise RenderDocError(f"SaveTexture failed: {result}")
        return {"saved": out_path, "resourceId": str(resid), "fileType": file_type}

    return json.dumps(session.run(_save), indent=2)


# ---------------------------------------------------------------------------
# Shader tools
# ---------------------------------------------------------------------------


@mcp.tool()
def get_disassembly_targets() -> str:
    """List available shader disassembly formats (e.g. DXBC, SPIR-V, GCN ISA)."""
    controller = session.require_controller()

    def _get() -> list:
        return list(controller.GetDisassemblyTargets(True))

    return json.dumps(session.run(_get), indent=2)


@mcp.tool()
def get_shader_disassembly(
    stage: str, event_id: Optional[int] = None, target: Optional[str] = None
) -> str:
    """Return disassembly for the shader bound at a stage.

    Args:
        stage: One of Vertex, Hull, Domain, Geometry, Pixel, Compute.
        event_id: Optional event to move to first.
        target: Disassembly target (see get_disassembly_targets). Defaults to
            the first available target.
    """
    if event_id is not None:
        session.set_event(event_id)
    controller = session.require_controller()

    def _get() -> dict:
        state = controller.GetPipelineState()
        stage_enum = _stage_enum(stage)
        refl = state.GetShaderReflection(stage_enum)
        if refl is None:
            raise RenderDocError(f"No shader bound at stage {stage}.")

        pipe = state.GetGraphicsPipelineObject()
        targets = controller.GetDisassemblyTargets(True)
        chosen = target or (targets[0] if targets else "")
        text = controller.DisassembleShader(pipe, refl, chosen)
        return {
            "stage": stage,
            "target": chosen,
            "availableTargets": list(targets),
            "disassembly": text,
        }

    return json.dumps(session.run(_get), indent=2)


@mcp.tool()
def get_shader_reflection(stage: str, event_id: Optional[int] = None) -> str:
    """Return reflection info for a shader: inputs, outputs, constant blocks, resources."""
    if event_id is not None:
        session.set_event(event_id)
    controller = session.require_controller()

    def _get() -> dict:
        state = controller.GetPipelineState()
        stage_enum = _stage_enum(stage)
        refl = state.GetShaderReflection(stage_enum)
        if refl is None:
            raise RenderDocError(f"No shader bound at stage {stage}.")

        def sig(s: Any) -> dict:
            return {
                "name": s.varName if hasattr(s, "varName") else getattr(s, "semanticName", ""),
                "semantic": getattr(s, "semanticName", ""),
                "index": int(getattr(s, "semanticIndex", 0)),
                "compType": _enum(getattr(s, "compType", "")),
                "components": int(getattr(s, "compCount", 0)),
            }

        result: dict = {
            "stage": stage,
            "resourceId": _rid(refl.resourceId),
            "entryPoint": getattr(refl, "entryPoint", ""),
            "inputs": [sig(s) for s in refl.inputSignature],
            "outputs": [sig(s) for s in refl.outputSignature],
            "constantBlocks": [
                {
                    "name": cb.name,
                    "byteSize": int(getattr(cb, "byteSize", 0)),
                    "variableCount": len(cb.variables),
                    "bufferBacked": bool(getattr(cb, "bufferBacked", True)),
                }
                for cb in refl.constantBlocks
            ],
            "readOnlyResources": [
                {"name": r.name, "type": _enum(getattr(r, "textureType", ""))}
                for r in refl.readOnlyResources
            ],
            "readWriteResources": [
                {"name": r.name, "type": _enum(getattr(r, "textureType", ""))}
                for r in refl.readWriteResources
            ],
            "samplers": [{"name": s.name} for s in refl.samplers],
        }
        return result

    return json.dumps(session.run(_get), indent=2)


@mcp.tool()
def get_constant_buffer(
    stage: str, slot: int = 0, event_id: Optional[int] = None
) -> str:
    """Read the constant/uniform buffer values bound to a shader stage.

    Args:
        stage: One of Vertex, Hull, Domain, Geometry, Pixel, Compute.
        slot: Index into the shader's constant block array.
        event_id: Optional event to move to first.
    """
    if event_id is not None:
        session.set_event(event_id)
    controller = session.require_controller()
    module = _rd()

    def _get() -> dict:
        state = controller.GetPipelineState()
        stage_enum = _stage_enum(stage)
        refl = state.GetShaderReflection(stage_enum)
        if refl is None:
            raise RenderDocError(f"No shader bound at stage {stage}.")
        if slot >= len(refl.constantBlocks):
            raise RenderDocError(
                f"Constant block slot {slot} out of range (have {len(refl.constantBlocks)})."
            )

        pipe = state.GetGraphicsPipelineObject()
        entry = state.GetShaderEntryPoint(stage_enum)
        cb = state.GetConstantBlock(stage_enum, slot, 0)

        cbufferVars = controller.GetCBufferVariableContents(
            pipe, refl.resourceId, stage_enum, entry, slot, cb.descriptor.resource, 0, 0
        )

        def var_to_dict(v: Any) -> dict:
            node: dict = {"name": v.name, "type": _enum(getattr(v, "type", ""))}
            if len(v.members) > 0:
                node["members"] = [var_to_dict(m) for m in v.members]
            else:
                rows, cols = int(v.rows), int(v.columns)
                vals = []
                for r in range(max(rows, 1)):
                    row = []
                    for c in range(max(cols, 1)):
                        idx = r * cols + c if cols else r
                        try:
                            row.append(float(v.value.f32v[idx]))
                        except Exception:  # noqa: BLE001
                            try:
                                row.append(int(v.value.s32v[idx]))
                            except Exception:  # noqa: BLE001
                                row.append(None)
                    vals.append(row if cols > 1 else (row[0] if row else None))
                node["value"] = vals if rows > 1 else (vals[0] if vals else None)
            return node

        return {
            "stage": stage,
            "slot": slot,
            "block": refl.constantBlocks[slot].name,
            "variables": [var_to_dict(v) for v in cbufferVars],
        }

    return json.dumps(session.run(_get), indent=2, default=str)


# ---------------------------------------------------------------------------
# GPU counter tools
# ---------------------------------------------------------------------------


@mcp.tool()
def list_counters() -> str:
    """Enumerate the GPU performance counters supported by this replay backend."""
    controller = session.require_controller()

    def _list() -> list:
        out = []
        for c in controller.EnumerateCounters():
            desc = controller.DescribeCounter(c)
            out.append(
                {
                    "counter": _enum(c),
                    "name": desc.name,
                    "description": desc.description,
                    "unit": _enum(desc.unit),
                    "resultType": _enum(desc.resultType),
                    "resultByteWidth": int(desc.resultByteWidth),
                }
            )
        return out

    return json.dumps(session.run(_list), indent=2)


@mcp.tool()
def fetch_counters(counters: list[str], event_ids: Optional[list[int]] = None) -> str:
    """Sample GPU counters across the frame.

    Args:
        counters: Counter names from list_counters (e.g. ["EventGPUDuration",
            "SamplesPassed"]). These map to the GPUCounter enum. Alias
            "GPUDuration" is accepted and resolved to EventGPUDuration.
        event_ids: If provided, only return results for these events.

    Returns per-event counter values (numeric).
    """
    controller = session.require_controller()
    module = _rd()

    def _fetch() -> list:
        aliases = {
            "GPUDuration": "EventGPUDuration",
            "Duration": "EventGPUDuration",
            "EventDuration": "EventGPUDuration",
        }
        available = list(controller.EnumerateCounters())
        avail_ids = set()
        for c in available:
            try:
                avail_ids.add(int(c))
            except Exception:
                try:
                    avail_ids.add(int(getattr(c, "value", 0)))
                except Exception:
                    pass

        counter_enums = []
        for name in counters:
            key = str(name).split(".")[-1]
            key = aliases.get(key, key)
            c = None
            try:
                c = getattr(module.GPUCounter, key)
            except AttributeError:
                c = None
            if c is not None and available:
                try:
                    cid = int(c)
                except Exception:
                    cid = int(getattr(c, "value", -1))
                if cid not in avail_ids:
                    # Match by numeric id among EnumerateCounters results.
                    c = next((x for x in available if int(x) == cid), None)
            if c is None and key in ("EventGPUDuration", "GPUDuration", "Duration"):
                c = next((x for x in available if int(x) == 1), None)
            if c is None:
                raise RenderDocError(
                    f"Unknown or unavailable counter '{name}'. "
                    f"Use list_counters (duration is EventGPUDuration / id=1)."
                )
            if c not in counter_enums:
                counter_enums.append(c)

        descs = {c: controller.DescribeCounter(c) for c in counter_enums}
        results = controller.FetchCounters(counter_enums)

        wanted = set(int(e) for e in event_ids) if event_ids else None
        out = []
        for r in results:
            if wanted is not None and int(r.eventId) not in wanted:
                continue
            desc = descs.get(r.counter)
            value: Any
            if desc is not None and desc.resultType == module.CompType.Float:
                value = float(r.value.d) if desc.resultByteWidth == 8 else float(r.value.f)
            elif desc is not None and desc.resultByteWidth == 8:
                value = int(r.value.u64)
            else:
                value = int(r.value.u32)
            out.append(
                {
                    "eventId": int(r.eventId),
                    "counter": _enum(r.counter),
                    "value": value,
                }
            )
        return out

    return json.dumps(session.run(_fetch), indent=2)


# ---------------------------------------------------------------------------
# Hot-question playbook (shared with in-UI panel)
# ---------------------------------------------------------------------------


def _playbook_get_current_frame() -> str:
    """Synthesize panel-compatible current-frame JSON for MCP backends."""
    controller = session.require_controller()

    def _get() -> dict:
        props = controller.GetAPIProperties()
        eid = int(session.current_event)
        action = None
        try:
            action = session.action_for_event(eid)
        except Exception:  # noqa: BLE001
            action = None
        names = _resource_name_map()
        data: dict = {
            "api": _enum(props.pipelineType),
            "localRenderer": _enum(props.localRenderer),
            "currentEvent": eid,
            "totalActions": len(session.all_actions()),
            "action": _action_summary(action, names) if action is not None else None,
            "pipeline": None,
        }
        return data

    return json.dumps(session.run(_get), indent=2)


def _mcp_playbook_call(tool: str, args: Optional[dict] = None) -> str:
    """Dispatch a playbook collect step onto MCP tool functions."""
    args = args or {}
    if tool == "list_actions":
        return list_actions(
            parent_event_id=args.get("parent_event_id"),
            max_depth=int(args.get("max_depth") or 0),
            drawcalls_only=bool(args.get("drawcalls_only", False)),
        )
    if tool == "fetch_counters":
        return fetch_counters(
            counters=list(args.get("counters") or ["EventGPUDuration"]),
            event_ids=args.get("event_ids"),
        )
    if tool == "list_counters":
        return list_counters()
    if tool == "get_pipeline_state":
        return get_pipeline_state(event_id=args.get("event_id"))
    if tool == "get_shader_disassembly":
        return get_shader_disassembly(
            stage=str(args.get("stage") or "Pixel"),
            event_id=args.get("event_id"),
            target=args.get("target"),
        )
    if tool == "get_shader_reflection":
        return get_shader_reflection(
            stage=str(args.get("stage") or "Pixel"),
            event_id=args.get("event_id"),
        )
    if tool == "list_textures":
        return list_textures(name_filter=args.get("name_filter"))
    if tool == "list_resources":
        return list_resources(name_filter=args.get("name_filter"))
    if tool == "get_action":
        return get_action(event_id=int(args["event_id"]))
    if tool == "get_event_chunk":
        return get_event_chunk(event_id=int(args["event_id"]))
    if tool == "get_capture_info":
        return get_capture_info()
    if tool == "get_status":
        return get_status()
    if tool == "get_current_frame":
        return _playbook_get_current_frame()
    try:
        from renderdoc_mcp import deep_inspect as _deep  # type: ignore
    except ImportError:
        import deep_inspect as _deep  # type: ignore
    fn = (_deep.DISPATCH or {}).get(tool)
    if fn is not None:
        import inspect
        params = inspect.signature(fn).parameters
        kwargs = {k: v for k, v in args.items() if k in params and v is not None}
        return fn(**kwargs)
    raise RenderDocError("Playbook backend does not support tool '%s'" % tool)


@mcp.tool()
def list_hot_questions(tag: Optional[str] = None) -> str:
    """List hot analysis questions from the shared playbook (id/title/tags/hot).

    Prefer ``run_question`` after picking an id. Use ``tag`` to filter
    (e.g. \"耗时\", \"shader\", \"sync\").
    """
    try:
        from renderdoc_mcp.playbook import list_questions  # type: ignore
    except ImportError:
        from playbook import list_questions  # type: ignore

    rows = []
    for q in list_questions(path="mcp", tag=tag):
        rows.append(
            {
                "id": q["id"],
                "title": q.get("title"),
                "tags": q.get("tags") or [],
                "hot": q.get("hot"),
                "followups": q.get("followups") or [],
            }
        )
    return json.dumps(rows, ensure_ascii=False, indent=2)


@mcp.tool()
def describe_hot_question(question_id: str) -> str:
    """Describe one playbook question: collect steps, analyzer, followups."""
    try:
        from renderdoc_mcp.playbook import describe_question  # type: ignore
    except ImportError:
        from playbook import describe_question  # type: ignore

    info = describe_question(question_id)
    if info is None:
        return json.dumps({"error": "unknown question_id: %s" % question_id}, ensure_ascii=False)
    return json.dumps(info, ensure_ascii=False, indent=2)


@mcp.tool()
def run_question(question_id: str, params: Optional[dict] = None) -> str:
    """Run a hot question locally: collect RenderDoc data + rule-based report.

    Does not call an external LLM. Load a capture first with ``load_capture``.
    Optional ``params`` may include ``event_id``, ``top_n``, etc.
    """
    try:
        from renderdoc_mcp.playbook import (  # type: ignore
            CallableBackend,
            format_result,
            run_question as _run,
        )
    except ImportError:
        from playbook import (  # type: ignore
            CallableBackend,
            format_result,
            run_question as _run,
        )

    backend = CallableBackend(_mcp_playbook_call)
    result = _run(question_id, backend, params=params)
    payload = dict(result)
    payload["text"] = format_result(result)
    return json.dumps(payload, ensure_ascii=False, indent=2)


@mcp.tool()
def analyze_question(text: str, params: Optional[dict] = None) -> str:
    """Auto-analyze a natural-language question about the loaded capture.

    Routes intent → tool plan → RenderDoc APIs → local report (no LLM).
    Prefer this for free-form questions; use ``run_question`` for known playbook ids.
    Optional ``params`` may include ``event_id``, ``top_n``, ``explain_with_llm``.
    """
    try:
        from renderdoc_mcp.playbook import CallableBackend  # type: ignore
        from renderdoc_mcp.orchestrator import answer as _answer  # type: ignore
    except ImportError:
        from playbook import CallableBackend  # type: ignore
        from orchestrator import answer as _answer  # type: ignore

    backend = CallableBackend(_mcp_playbook_call)
    result = _answer(text, backend, path="mcp", params=params or {})
    # Compact JSON for MCP clients; ``text`` is the human-readable report.
    payload = {
        "kind": result.get("kind"),
        "intent": result.get("intent"),
        "question_id": result.get("question_id"),
        "title": result.get("title"),
        "analyze": result.get("analyze"),
        "explain_with_llm": result.get("explain_with_llm"),
        "slots": result.get("slots"),
        "steps": result.get("steps"),
        "errors": result.get("errors"),
        "followups": result.get("followups"),
        "text": result.get("text") or "",
        "report": result.get("report"),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def main() -> None:
    try:
        mcp.run()
    finally:
        session.shutdown()


if __name__ == "__main__":
    main()
