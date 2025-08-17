# recorder/events.py
from pydantic import BaseModel, Field
from typing import Optional, Dict, Any, Literal
from datetime import datetime

EventType = Literal[
    "nav", "click", "input", "keydown", "change", "submit", "visibility"
]

class SelectorInfo(BaseModel):
    css: Optional[str] = None
    xpath: Optional[str] = None

class ElementInfo(BaseModel):
    tag: Optional[str] = None
    id: Optional[str] = None
    classes: Optional[str] = None
    name: Optional[str] = None
    type: Optional[str] = None
    role: Optional[str] = None
    aria_label: Optional[str] = Field(None, alias="ariaLabel")
    title: Optional[str] = None
    text: Optional[str] = None  # small/trimmed
    value_preview: Optional[str] = None  # masked for passwords
    selectors: Optional[SelectorInfo] = None

class BaseEvent(BaseModel):
    t: str = Field(default_factory=lambda: datetime.utcnow().isoformat() + "Z")
    etype: EventType
    url: Optional[str] = None
    meta: Dict[str, Any] = Field(default_factory=dict)

class ClickEvent(BaseEvent):
    etype: Literal["click"] = "click"
    x: int
    y: int
    button: Literal["left", "middle", "right"] = "left"
    el: Optional[ElementInfo] = None

class InputEvent(BaseEvent):
    etype: Literal["input"] = "input"
    el: Optional[ElementInfo] = None
    input_value: Optional[str] = None  # masked if password

class KeyEvent(BaseEvent):
    etype: Literal["keydown"] = "keydown"
    key: str
    code: str
    ctrl: bool
    alt: bool
    shift: bool
    meta_key: bool

class ChangeEvent(BaseEvent):
    etype: Literal["change"] = "change"
    el: Optional[ElementInfo] = None
    value: Optional[str] = None

class SubmitEvent(BaseEvent):
    etype: Literal["submit"] = "submit"
    el: Optional[ElementInfo] = None

class NavEvent(BaseEvent):
    etype: Literal["nav"] = "nav"
    from_url: Optional[str] = None
    to_url: Optional[str] = None

class VisibilityEvent(BaseEvent):
    etype: Literal["visibility"] = "visibility"
    state: Literal["visible", "hidden"]
