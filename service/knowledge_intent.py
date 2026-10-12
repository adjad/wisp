"""Pure knowledge request grammar shared by the entry boundary and router."""
from __future__ import annotations

from dataclasses import dataclass
import re

from service.utterance_shape import is_mention, is_prohibition, quoted_spans


@dataclass(frozen=True)
class KnowledgeEnvelope:
    tools: tuple[str, ...]
    bindings: dict[str, dict]


_CATEGORY = r'''(?:knowledge|style|follow-up)(?=$|[\s:"”'`]|[.!?,](?:\s|$))'''
_CATEGORY_LABEL = r'''["“'`]?''' + _CATEGORY
_ATTEMPT = re.compile(
    r"^\s*(?:please\s+)?(?:(?:can|could|would)\s+you\s+(?:please\s+)?)?"
    r"(?P<verb>save|store|remember|import|read|show|list|update|edit|replace|delete|remove|forget)"
    r"\s+(?P<rest>[\s\S]*)", re.I)


def _addressed_attempt(text: str) -> bool:
    """Recognize bounded invalid instructions for clarification, never execution.

    A quoted whole subject is data; an immediate quoted destination label is an
    unsupported label. Source descriptors need not be executable source enums.
    A pre-destination title field is ordinary source data, not an import cue.
    """
    command = _ATTEMPT.match(text)
    if not command:
        return False
    rest = command['rest']
    if re.match(r"(?:(?:my|saved|a|an|the)\s+){0,3}" + _CATEGORY_LABEL, rest, re.I):
        return True
    if command['verb'].lower() not in {'save', 'store', 'remember', 'import'}:
        return False
    if re.match(r"to\b", rest, re.I):
        return False
    spans = quoted_spans(rest)
    def unquoted(offset: int) -> bool:
        return not any(start <= offset < end for start, end in spans)

    # Recognize only destinations in the structural header. A standalone title
    # marker closes it, except at an unambiguous opaque source/container ID slot.
    # In particular, neither mail-titled-1 nor <titled@example> is a delimiter.
    tokens = list(re.finditer(r"\S+", rest))
    words = [m.group().lower() for m in tokens]
    source = 1 if words and words[0] == 'my' else 0
    data_slots = {source}
    container = None
    if len(words) > source + 2 and words[source + 2] == 'in':
        data_slots.add(source + 1)
        container = source + 2
    elif (len(words) > source + 2 and words[source + 1] == 'in'
          and words[source + 2].rstrip(':') != 'titled'):
        # Missing source ID. Preserve SOURCE + opaque ID "in" + titled TITLE
        # as the existing ordinary-source title shape instead.
        container = source + 1
    if container is not None and unquoted(tokens[container].start()):
        data_slots.update((container + 1, container + 2))
    boundary = len(rest)
    for index, token in enumerate(tokens):
        if (index not in data_slots and token.group().lower().rstrip(':') == 'titled'
                and unquoted(token.start())):
            boundary = token.start()
            break
    for body in re.finditer(r":\r?\n", rest):
        if unquoted(body.start()):
            boundary = min(boundary, body.start())
            break
    destination = r"\bas\s+(?:(?:a|an|the)\s+)?" + _CATEGORY_LABEL
    return any(m.start() < boundary and unquoted(m.start())
               for m in re.finditer(destination, rest, re.I))


def knowledge_envelope(text: str) -> KnowledgeEnvelope | None:
    """Recognize literal knowledge fields before interpreting their contents.

    An addressed malformed request returns an empty envelope for clarification.
    For a valid save/edit header, all bytes after its newline belong to the body.
    No tool, source, configuration, model or persisted state is accessed here.
    """
    surface = r"(?:knowledge(?:\s+notes?)?|style\s+examples?|follow-up\s+candidates?)"
    attempt = _addressed_attempt(text)
    if not attempt and not re.search(r"\b" + surface + r"\b", text, re.I):
        return None
    why = is_prohibition(text) or is_mention(text)
    value = text.strip()
    prefix = r"(?:please\s+)?"
    labels = {'knowledge note':'knowledge', 'style example':'style_example',
              'follow-up candidate':'followup_candidate'}
    label = r"(?P<kind>knowledge\s+note|style\s+example|follow-up\s+candidate)"
    saved = re.fullmatch(prefix + r"(?:save|store|remember)\s+(?:a\s+)?" + label
        + r"\s+titled\s+(?P<title>[^\r\n]+):\r?\n(?P<body>[\s\S]+)", text.lstrip(), re.I)
    imported = re.fullmatch(prefix + r"(?:save|store)\s+(?P<source>email|messages)\s+"
        r"(?P<id>[^\s]+)\s+in\s+(?P<container_label>account|chat)\s+(?P<container>[^\s]+)"
        r"\s+as\s+(?:a\s+)?" + label + r"\s+titled\s+(?P<title>[^\r\n]+)", value, re.I)
    read = re.fullmatch(prefix + r"(?:read|show|list)\s+(?:my\s+)?(?:saved\s+)?"
        + r'(?P<kind>' + surface + r")(?:\s+(?P<id>(?-i:[0-9a-f]{32})))?[.!?]?", value, re.I)
    edit = re.fullmatch(prefix + r"(?:update|edit|replace)\s+knowledge\s+"
        r"(?P<id>(?-i:[0-9a-f]{32}))\s+titled\s+(?P<title>[^\r\n]+):\r?\n(?P<body>[\s\S]+)", text.lstrip(), re.I)
    delete = re.fullmatch(prefix + r"(?:delete|remove|forget)\s+knowledge\s+"
        r"(?P<id>(?-i:[0-9a-f]{32}))\.?", value, re.I)
    tools: list[str] = []
    bindings: dict[str, dict] = {}
    if not why and saved:
        tools = ['save_knowledge']
        bindings = {'save_knowledge':{'title':saved['title'], 'text':saved['body'],
                    'kind':labels[re.sub(r'\s+', ' ', saved['kind'].lower())],
                    'source_type':'user_request', 'source_id':'', 'container_id':''}}
    elif not why and imported and imported['container_label'].lower() == (
            'account' if imported['source'].lower() == 'email' else 'chat'):
        tools = ['save_knowledge']
        bindings = {'save_knowledge':{'title':imported['title'], 'text':'',
            'kind':labels[re.sub(r'\s+', ' ', imported['kind'].lower())],
            'source_type':imported['source'].lower(), 'source_id':imported['id'],
            'container_id':imported['container']}}
    elif not why and read:
        tools = ['read_knowledge']
        kind = ('style_example' if read['kind'].lower().startswith('style') else
                'followup_candidate' if read['kind'].lower().startswith('follow-up') else
                '' if read['kind'].lower() == 'knowledge' else 'knowledge')
        bindings = {'read_knowledge':{'record_id':read['id'] or '', 'kind':kind}}
    elif not why and (edit or delete):
        match = edit or delete
        effect = 'edit_knowledge' if edit else 'delete_knowledge'
        tools = ['read_knowledge', effect]
        bindings = {'read_knowledge':{'record_id':match['id']}, effect:{'record_id':match['id']}}
        if edit:
            bindings[effect].update(title=edit['title'], text=edit['body'])
    else:
        # Only claim explicitly addressed incomplete instructions or utterance
        # shapes. Other knowledge questions retain ordinary interpretation.
        if not why and not (attempt or imported):
            return None
    return KnowledgeEnvelope(tuple(tools), bindings)
