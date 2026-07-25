"""Deep inspection MCP tools wrapping IReplayController APIs.

Registered onto the FastMCP instance from server.register_deep_tools().
"""
from __future__ import annotations

import base64
import json
from typing import Any, Callable, Optional

# name -> callable for playbook / orchestrator dispatch
DISPATCH: dict[str, Callable[..., str]] = {}


def register_deep_tools(mcp, session, RenderDocError, _rd, _rid, _enum, _resource_name_map):
    """Attach Top-N deep inspect tools to ``mcp``."""

    def _normalize_rid_str(resource_id: str) -> str:
        wanted = str(resource_id).strip()
        if not wanted.startswith("ResourceId::"):
            wanted = "ResourceId::%s" % wanted
        return wanted

    def _find_resource_id(wanted: str):
        """Resolve a string id against GetResources / textures / buffers."""
        controller = session.require_controller()
        wanted = _normalize_rid_str(wanted)
        for res in controller.GetResources():
            if str(res.resourceId) == wanted:
                return res.resourceId
        for tex in controller.GetTextures():
            if str(tex.resourceId) == wanted:
                return tex.resourceId
        for buf in controller.GetBuffers():
            if str(buf.resourceId) == wanted:
                return buf.resourceId
        raise RenderDocError("Resource id '%s' not found." % wanted)

    def _subresource(module, mip=0, slice_index=0, sample=0):
        try:
            return module.Subresource(int(mip), int(slice_index), int(sample))
        except Exception:
            sub = module.Subresource()
            sub.mip = int(mip)
            sub.slice = int(slice_index)
            sub.sample = int(sample)
            return sub

    def _comp_type(module, name: Optional[str]):
        name = (name or "Typeless").strip()
        try:
            return getattr(module.CompType, name)
        except AttributeError as exc:
            raise RenderDocError(
                "Unknown CompType '%s'. Common: Typeless, Float, UNorm, SNorm, UInt, SInt"
                % name
            ) from exc

    def _pixel_value_dict(pv: Any) -> dict:
        out: dict = {}
        try:
            out["float"] = [float(x) for x in pv.floatValue]
        except Exception:
            pass
        try:
            out["uint"] = [int(x) for x in pv.uintValue]
        except Exception:
            pass
        try:
            out["sint"] = [int(x) for x in pv.intValue]
        except Exception:
            try:
                out["sint"] = [int(x) for x in pv.sintValue]
            except Exception:
                pass
        return out

    def _mod_colour(mod_obj: Any) -> dict:
        data: dict = {}
        try:
            data["col"] = _pixel_value_dict(mod_obj.col)
        except Exception:
            pass
        try:
            data["depth"] = float(mod_obj.depth)
        except Exception:
            pass
        try:
            data["stencil"] = int(mod_obj.stencil)
        except Exception:
            pass
        return data

    def _modification_dict(m: Any) -> dict:
        data = {
            "eventId": int(m.eventId),
            "primitiveID": int(getattr(m, "primitiveID", 0)),
            "fragIndex": int(getattr(m, "fragIndex", 0)),
            "directShaderWrite": bool(getattr(m, "directShaderWrite", False)),
            "unboundPS": bool(getattr(m, "unboundPS", False)),
        }
        try:
            data["passed"] = bool(m.Passed())
        except Exception:
            data["passed"] = None
        for flag in (
            "sampleMasked",
            "backfaceCulled",
            "depthClipped",
            "depthBoundsFailed",
            "viewClipped",
            "scissorClipped",
            "shaderDiscarded",
            "depthTestFailed",
            "stencilTestFailed",
        ):
            if hasattr(m, flag):
                data[flag] = bool(getattr(m, flag))
        try:
            data["preMod"] = _mod_colour(m.preMod)
        except Exception:
            pass
        try:
            data["shaderOut"] = _mod_colour(m.shaderOut)
        except Exception:
            pass
        try:
            data["postMod"] = _mod_colour(m.postMod)
        except Exception:
            pass
        return data

    def _descriptor_dict(d: Any, names: dict) -> dict:
        rid = _rid(getattr(d, "resource", None))
        out = {
            "type": _enum(getattr(d, "type", "")),
            "resource": rid,
            "resourceName": names.get(rid or "", ""),
            "byteOffset": int(getattr(d, "byteOffset", 0)),
            "byteSize": int(getattr(d, "byteSize", 0)),
            "firstMip": int(getattr(d, "firstMip", 0)),
            "numMips": int(getattr(d, "numMips", 0)),
            "firstSlice": int(getattr(d, "firstSlice", 0)),
            "numSlices": int(getattr(d, "numSlices", 0)),
        }
        try:
            fmt = getattr(d, "format", None)
            if fmt is not None:
                out["format"] = _enum(getattr(fmt, "Name", lambda: fmt)() if callable(getattr(fmt, "Name", None)) else getattr(fmt, "type", fmt))
        except Exception:
            pass
        return out

    # ------------------------------------------------------------------
    # Tools
    # ------------------------------------------------------------------

    @mcp.tool()
    def get_resource_usage(resource_id: str) -> str:
        """List events that read/write a resource (GetUsage).

        Critical for black-screen / "who wrote this RT" questions.
        Args:
            resource_id: From list_textures/list_resources (e.g. "47" or "ResourceId::47").
        """
        controller = session.require_controller()

        def _get() -> dict:
            resid = _find_resource_id(resource_id)
            names = _resource_name_map()
            usages = []
            for u in controller.GetUsage(resid):
                usages.append({
                    "eventId": int(u.eventId),
                    "usage": _enum(u.usage),
                })
            return {
                "resourceId": str(resid),
                "name": names.get(str(resid), ""),
                "usages": usages,
                "count": len(usages),
            }

        return json.dumps(session.run(_get), indent=2)

    @mcp.tool()
    def pick_pixel(
        resource_id: str,
        x: int,
        y: int,
        event_id: Optional[int] = None,
        mip: int = 0,
        slice_index: int = 0,
        sample: int = 0,
        type_cast: str = "Typeless",
    ) -> str:
        """Sample one texel (PickPixel). Coords are top-left origin."""
        if event_id is not None:
            session.set_event(event_id)
        controller = session.require_controller()
        module = _rd()

        def _get() -> dict:
            resid = _find_resource_id(resource_id)
            sub = _subresource(module, mip, slice_index, sample)
            cast = _comp_type(module, type_cast)
            val = controller.PickPixel(resid, int(x), int(y), sub, cast)
            return {
                "resourceId": str(resid),
                "x": int(x),
                "y": int(y),
                "mip": int(mip),
                "slice": int(slice_index),
                "sample": int(sample),
                "typeCast": type_cast,
                "value": _pixel_value_dict(val),
                "eventId": int(session.current_event),
            }

        return json.dumps(session.run(_get), indent=2)

    @mcp.tool()
    def get_pixel_history(
        resource_id: str,
        x: int,
        y: int,
        event_id: Optional[int] = None,
        mip: int = 0,
        slice_index: int = 0,
        sample: int = 0,
        type_cast: str = "Typeless",
        max_mods: int = 100,
    ) -> str:
        """Per-pixel modification history (PixelHistory). Can be slow on large frames."""
        if event_id is not None:
            session.set_event(event_id)
        controller = session.require_controller()
        module = _rd()

        def _get() -> dict:
            resid = _find_resource_id(resource_id)
            sub = _subresource(module, mip, slice_index, sample)
            cast = _comp_type(module, type_cast)
            mods = list(controller.PixelHistory(resid, int(x), int(y), sub, cast))
            limit = max(1, int(max_mods))
            truncated = len(mods) > limit
            shown = mods[:limit]
            return {
                "resourceId": str(resid),
                "x": int(x),
                "y": int(y),
                "totalMods": len(mods),
                "returned": len(shown),
                "truncated": truncated,
                "modifications": [_modification_dict(m) for m in shown],
            }

        return json.dumps(session.run(_get), indent=2)

    @mcp.tool()
    def get_texture_minmax(
        resource_id: str,
        event_id: Optional[int] = None,
        mip: int = 0,
        slice_index: int = 0,
        sample: int = 0,
        type_cast: str = "Typeless",
    ) -> str:
        """Min/max texel values in a texture subresource (GetMinMax)."""
        if event_id is not None:
            session.set_event(event_id)
        controller = session.require_controller()
        module = _rd()

        def _get() -> dict:
            resid = _find_resource_id(resource_id)
            sub = _subresource(module, mip, slice_index, sample)
            cast = _comp_type(module, type_cast)
            pair = controller.GetMinMax(resid, sub, cast)
            # Python may return tuple or rdcpair with first/second
            if isinstance(pair, (list, tuple)) and len(pair) >= 2:
                mn, mx = pair[0], pair[1]
            else:
                mn = getattr(pair, "first", getattr(pair, "Min", None))
                mx = getattr(pair, "second", getattr(pair, "Max", None))
            return {
                "resourceId": str(resid),
                "mip": int(mip),
                "slice": int(slice_index),
                "min": _pixel_value_dict(mn) if mn is not None else None,
                "max": _pixel_value_dict(mx) if mx is not None else None,
            }

        return json.dumps(session.run(_get), indent=2)

    @mcp.tool()
    def get_buffer_data(
        resource_id: str,
        offset: int = 0,
        length: int = 256,
        event_id: Optional[int] = None,
        encoding: str = "hex",
    ) -> str:
        """Read raw buffer bytes (GetBufferData). Default length capped at 4096.

        Args:
            encoding: "hex" (default), "base64", or "both".
            length: 0 means rest of buffer, but still hard-capped at 4096 for MCP.
        """
        if event_id is not None:
            session.set_event(event_id)
        controller = session.require_controller()
        hard_cap = 4096
        req_len = int(length)
        if req_len <= 0 or req_len > hard_cap:
            fetch_len = hard_cap if req_len <= 0 else hard_cap
        else:
            fetch_len = req_len

        def _get() -> dict:
            resid = _find_resource_id(resource_id)
            raw = bytes(controller.GetBufferData(resid, int(offset), int(fetch_len)))
            # If user asked 0 (=rest), note we capped
            out = {
                "resourceId": str(resid),
                "offset": int(offset),
                "requestedLength": int(length),
                "returnedBytes": len(raw),
                "capped": (req_len <= 0 or req_len > hard_cap or (req_len <= 0)),
                "hardCap": hard_cap,
            }
            enc = (encoding or "hex").lower()
            if enc in ("hex", "both"):
                out["hex"] = raw.hex()
            if enc in ("base64", "both"):
                out["base64"] = base64.b64encode(raw).decode("ascii")
            if enc not in ("hex", "base64", "both"):
                out["hex"] = raw.hex()
            return out

        return json.dumps(session.run(_get), indent=2)

    @mcp.tool()
    def get_debug_messages() -> str:
        """Fetch newly generated replay/API diagnostic messages (GetDebugMessages).

        Consuming: each call returns only messages since the previous call.
        """
        controller = session.require_controller()

        def _get() -> list:
            out = []
            for msg in controller.GetDebugMessages():
                out.append({
                    "eventId": int(getattr(msg, "eventId", 0)),
                    "category": _enum(getattr(msg, "category", "")),
                    "severity": _enum(getattr(msg, "severity", "")),
                    "source": _enum(getattr(msg, "source", "")),
                    "messageID": int(getattr(msg, "messageID", 0)),
                    "description": str(getattr(msg, "description", "")),
                })
            return out

        return json.dumps(session.run(_get), indent=2)

    @mcp.tool()
    def get_descriptor_access(event_id: Optional[int] = None) -> str:
        """Descriptors accessed at the current (or given) event (GetDescriptorAccess)."""
        if event_id is not None:
            session.set_event(event_id)
        controller = session.require_controller()

        def _get() -> dict:
            names = _resource_name_map()
            access = []
            for a in controller.GetDescriptorAccess():
                store = _rid(getattr(a, "descriptorStore", None))
                access.append({
                    "stage": _enum(getattr(a, "stage", "")),
                    "type": _enum(getattr(a, "type", "")),
                    "index": int(getattr(a, "index", 0)),
                    "arrayElement": int(getattr(a, "arrayElement", 0)),
                    "descriptorStore": store,
                    "storeName": names.get(store or "", ""),
                    "byteOffset": int(getattr(a, "byteOffset", 0)),
                    "byteSize": int(getattr(a, "byteSize", 0)),
                    "staticallyUnused": bool(getattr(a, "staticallyUnused", False)),
                })
            return {
                "eventId": int(session.current_event),
                "access": access,
                "count": len(access),
            }

        return json.dumps(session.run(_get), indent=2)

    @mcp.tool()
    def list_descriptor_stores() -> str:
        """List descriptor heaps/sets (GetDescriptorStores)."""
        controller = session.require_controller()

        def _get() -> list:
            names = _resource_name_map()
            out = []
            for s in controller.GetDescriptorStores():
                rid = str(s.resourceId)
                out.append({
                    "id": rid,
                    "name": names.get(rid, ""),
                    "descriptorByteSize": int(getattr(s, "descriptorByteSize", 0)),
                    "firstDescriptorOffset": int(getattr(s, "firstDescriptorOffset", 0)),
                    "descriptorCount": int(getattr(s, "descriptorCount", 0)),
                })
            return out

        return json.dumps(session.run(_get), indent=2)

    @mcp.tool()
    def get_descriptors(
        store_id: str,
        offset: int = 0,
        count: int = 16,
        descriptor_size: Optional[int] = None,
        descriptor_type: str = "Unknown",
        event_id: Optional[int] = None,
    ) -> str:
        """Read descriptor contents from a store range (GetDescriptors).

        Use list_descriptor_stores first. count is capped at 64.
        """
        if event_id is not None:
            session.set_event(event_id)
        controller = session.require_controller()
        module = _rd()
        count = max(1, min(int(count), 64))

        def _get() -> dict:
            store = _find_resource_id(store_id)
            # default size from store description if possible
            size = descriptor_size
            if size is None:
                size = 1
                for s in controller.GetDescriptorStores():
                    if str(s.resourceId) == str(store):
                        size = int(getattr(s, "descriptorByteSize", 1) or 1)
                        break
            rng = module.DescriptorRange()
            rng.offset = int(offset)
            rng.descriptorSize = int(size)
            rng.count = int(count)
            try:
                rng.type = getattr(module.DescriptorType, descriptor_type)
            except AttributeError:
                rng.type = module.DescriptorType.Unknown
            names = _resource_name_map()
            descs = controller.GetDescriptors(store, [rng])
            return {
                "storeId": str(store),
                "offset": int(offset),
                "count": int(count),
                "descriptorSize": int(size),
                "descriptors": [_descriptor_dict(d, names) for d in descs],
            }

        return json.dumps(session.run(_get), indent=2)

    @mcp.tool()
    def get_post_vs_data(
        event_id: Optional[int] = None,
        instance: int = 0,
        view: int = 0,
        stage: str = "VSOut",
    ) -> str:
        """Post-transform mesh buffer locations (GetPostVSData).

        Args:
            stage: MeshDataStage name — VSOut, GSOut, MeshOut, TaskOut, etc.
        """
        if event_id is not None:
            session.set_event(event_id)
        controller = session.require_controller()
        module = _rd()

        def _get() -> dict:
            try:
                stage_enum = getattr(module.MeshDataStage, stage)
            except AttributeError as exc:
                raise RenderDocError(
                    "Unknown MeshDataStage '%s'. Try VSOut, GSOut, MeshOut, TaskOut." % stage
                ) from exc
            mesh = controller.GetPostVSData(int(instance), int(view), stage_enum)
            return {
                "eventId": int(session.current_event),
                "stage": stage,
                "instance": int(instance),
                "view": int(view),
                "indexResourceId": _rid(getattr(mesh, "indexResourceId", None)),
                "indexByteOffset": int(getattr(mesh, "indexByteOffset", 0)),
                "indexByteStride": int(getattr(mesh, "indexByteStride", 0)),
                "indexByteSize": int(getattr(mesh, "indexByteSize", 0)),
                "baseVertex": int(getattr(mesh, "baseVertex", 0)),
                "vertexResourceId": _rid(getattr(mesh, "vertexResourceId", None)),
                "vertexByteOffset": int(getattr(mesh, "vertexByteOffset", 0)),
                "vertexByteStride": int(getattr(mesh, "vertexByteStride", 0)),
                "vertexByteSize": int(getattr(mesh, "vertexByteSize", 0)),
                "numIndices": int(getattr(mesh, "numIndices", 0)),
                "topo": _enum(getattr(mesh, "topology", getattr(mesh, "topo", ""))),
                "unproject": bool(getattr(mesh, "unproject", False)),
            }

        return json.dumps(session.run(_get), indent=2)

    @mcp.tool()
    def debug_pixel(
        x: int,
        y: int,
        event_id: Optional[int] = None,
        sample: Optional[int] = None,
        primitive: Optional[int] = None,
        max_steps: int = 32,
    ) -> str:
        """Debug the pixel shader at (x,y) (DebugPixel). Returns a compact trace summary.

        Coords are top-left. Full stepping is capped by max_steps.
        """
        if event_id is not None:
            session.set_event(event_id)
        controller = session.require_controller()
        module = _rd()
        max_steps = max(1, min(int(max_steps), 256))

        def _get() -> dict:
            inputs = module.DebugPixelInputs()
            if sample is not None:
                inputs.sample = int(sample)
            if primitive is not None:
                inputs.primitive = int(primitive)
            trace = controller.DebugPixel(int(x), int(y), inputs)
            if trace is None:
                return {
                    "eventId": int(session.current_event),
                    "x": int(x),
                    "y": int(y),
                    "ok": False,
                    "error": "DebugPixel returned None (unsupported or no fragment).",
                }
            try:
                has_dbg = getattr(trace, "debugger", None) is not None
                source_vars = []
                for sv in list(getattr(trace, "sourceVars", []) or [])[:40]:
                    source_vars.append({
                        "name": str(getattr(sv, "name", "")),
                        "signatureIndex": int(getattr(sv, "signatureIndex", -1)),
                    })
                steps = []
                total_states = 0
                if has_dbg:
                    debugger = trace.debugger
                    while total_states < max_steps:
                        states = list(controller.ContinueDebug(debugger) or [])
                        if not states:
                            break
                        for st in states:
                            total_states += 1
                            steps.append({
                                "stepIndex": int(getattr(st, "stepIndex", total_states - 1)),
                                "flags": _enum(getattr(st, "flags", "")),
                            })
                            if total_states >= max_steps:
                                break
                result = {
                    "eventId": int(session.current_event),
                    "x": int(x),
                    "y": int(y),
                    "ok": True,
                    "hasDebugger": has_dbg,
                    "sourceVars": source_vars,
                    "stepsCollected": total_states,
                    "stepsTruncated": total_states >= max_steps,
                    "steps": steps[:16],
                }
            finally:
                try:
                    controller.FreeTrace(trace)
                except Exception:
                    pass
            return result

        return json.dumps(session.run(_get), indent=2)

    DISPATCH.clear()
    DISPATCH.update({
        "get_resource_usage": get_resource_usage,
        "pick_pixel": pick_pixel,
        "get_pixel_history": get_pixel_history,
        "get_texture_minmax": get_texture_minmax,
        "get_buffer_data": get_buffer_data,
        "get_debug_messages": get_debug_messages,
        "get_descriptor_access": get_descriptor_access,
        "list_descriptor_stores": list_descriptor_stores,
        "get_descriptors": get_descriptors,
        "get_post_vs_data": get_post_vs_data,
        "debug_pixel": debug_pixel,
    })
    return list(DISPATCH.keys())
