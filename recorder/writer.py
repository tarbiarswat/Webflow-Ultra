# recorder/writer.py
import os
from datetime import datetime
import orjson
from typing import Any, Dict

class JsonlWriter:
    def __init__(self, out_dir: str):
        os.makedirs(out_dir, exist_ok=True)
        stamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
        self.path = os.path.join(out_dir, f"session-{stamp}.jsonl")
        self._f = open(self.path, "ab")
        self.count = 0

    def write(self, event_obj: Dict[str, Any]):
        self._f.write(orjson.dumps(event_obj))
        self._f.write(b"\n")
        self._f.flush()
        self.count += 1
        if self.count <= 3:   # <— debug: confirms events are flowing
            print(f"[writer] events={self.count}")

    def close(self):
        try:
            self._f.flush()
        finally:
            self._f.close()
