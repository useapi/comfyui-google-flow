"""Google Flow nodes for ComfyUI (Veo 3.1, Omni 1.2 Flash, Nano Banana) through the useapi.net API."""

if __package__:  # ComfyUI imports this folder as a package; pytest imports this file alone while collecting tests
    from .nodes import NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS
else:
    NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS = {}, {}

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]
