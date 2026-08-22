import os
import json


class OrderableRegistry:
    """A list of registered items, orderable by the user and persisted
    to disk. Registration order (with built-in items always sorted
    before non-built-in ones) is just the default/fallback -- a saved
    user order always wins once one exists.
    """

    def __init__(self, order_file):
        self._items = []
        self._order_file = order_file

    def register(self, key, built_in=False, **data):
        self._items.append({"key": key, "built_in": built_in, **data})

    def all_items(self):
        return list(self._items)

    def ordered_items(self):
        saved = self._load_saved_order()
        by_key = {item["key"]: item for item in self._items}
        ordered = [by_key[k] for k in saved if k in by_key]
        remaining = [item for item in self._items if item["key"] not in saved]
        # Default fallback for anything with no saved position: built-in
        # items first, addon items after -- stable within each group.
        # Items with 'after' parameter are positioned after their target.
        built_ins = [item for item in remaining if item.get("built_in")]
        addons = [item for item in remaining if not item.get("built_in")]
        
        # Sort addons respecting 'after' constraints
        result = built_ins.copy()
        positioned = set()
        for item in addons:
            after_key = item.get("after")
            if after_key and after_key in by_key:
                # Find where to insert this item
                try:
                    idx = result.index(by_key[after_key])
                    result.insert(idx + 1, item)
                    positioned.add(item["key"])
                except ValueError:
                    pass
        
        # Add remaining addons that didn't have a valid 'after' target
        for item in addons:
            if item["key"] not in positioned:
                result.append(item)
        
        return ordered + result

    def save_order(self, keys):
        try:
            os.makedirs(os.path.dirname(self._order_file), exist_ok=True)
            with open(self._order_file, "w", encoding="utf-8") as f:
                json.dump(list(keys), f)
        except Exception:
            pass

    def _load_saved_order(self):
        try:
            with open(self._order_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except Exception:
            return []
