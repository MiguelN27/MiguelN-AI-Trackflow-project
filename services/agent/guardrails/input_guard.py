"""The input guard: every message is inspected before any model, tool or memory sees it.

Deterministic - regular expressions over the folded text (`normalize.fold`),
in English and Spanish, the agent's two languages. It decides one of:

    block      an attempt to change, remove or reveal the agent's instructions
               (security): a firm, fixed refusal; nothing else runs;
    refuse     a personal-chatbot request, or an order that is not the signed-in
               customer's (content); or an unreadable message (structural):
               a fixed refusal with the redirect; nothing else runs;
    constrain  a request to apply another country's policy to an order: the run
               goes on, locked to the order's real country (content);
    redirect   small talk or a general logistics question: a brief answer, then
               the redirect to what the agent is for (content);
    allow      everything else - TrackFlow's domain.

The precedence is structural, then security, then content: a message that
both asks for an essay and tells the agent to forget TrackFlow is an
instruction change first. The guard is the first layer, not the only one: the
hardened prompt, the model's own `scope` label, the isolation of external text
and the output check stand behind it (`docs/agent/agent-harness.md`).

Rules are kept in families with stable ids (`override.ignore_instructions`,
`personal.essay`, ...); the id is what the guardrail log and the summary count.
"""

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Final, Literal

from services.agent.guardrails.normalize import fold, visible

Action = Literal["allow", "redirect", "constrain", "refuse", "block"]
Category = Literal[
    "in_domain",
    "small_talk",
    "general_logistics",
    "policy_mixing",
    "personal_task",
    "unauthorized_order",
    "instruction_change",
    "malformed",
]
FailureType = Literal["structural", "content", "security"]

# Longer than the API accepts (1000) and than any real question: the CLI has no limit.
MAX_MESSAGE_CHARS: Final = 4000

_FLAGS: Final = re.IGNORECASE | re.MULTILINE


@dataclass(frozen=True)
class Rule:
    id: str
    pattern: re.Pattern[str]


def _rules(family: str, patterns: Iterable[tuple[str, str]]) -> tuple[Rule, ...]:
    return tuple(Rule(f"{family}.{name}", re.compile(regex, _FLAGS)) for name, regex in patterns)


# A clause starts here: the beginning of a line, after punctuation, or after the
# words that introduce an order ("please", "I want you to", ...). Used where a
# verb is only an attack in the imperative ("Act as ..." vs "TrackFlow acts as").
_CLAUSE: Final = (
    r"(?:^|[.!?;:,\"(]\s*|\b(?:please|pls|now|just|so|and|then|first|also|ok|okay|hey|hi|"
    r"well|but|actually|from now on|for now|for this (?:chat|conversation)|you (?:must|should|"
    r"can|will|shall|need to|have to)|i (?:want|need) you to|can you|could you|would you|"
    r"kindly|let'?s)\s+)"
)

# --- Security: instruction changes (5 families) ---------------------------------------

_OVERRIDE_VERBS: Final = (
    r"(?:ignore|disregard|forget|override|overwrite|bypass|circumvent|skip|discard|abandon|"
    r"set aside|stop following|stop obeying|do not follow|don'?t follow|no longer follow|"
    r"get around|throw away|neglect|disobey)"
)
_OWN_QUALIFIERS: Final = (
    r"(?:your|ur|yours|you'?ve been given|you were given|system|hidden|internal|initial|"
    r"original|built[- ]in|trackflow'?s?)"
)
_ANY_QUALIFIERS: Final = (
    r"(?:previous|prior|above|earlier|preceding|all|any|every|these|those|current|existing|"
    r"old|default|the above)"
)
_TARGETS: Final = (
    r"(?:instructions?|rules?|prompts?|guidelines?|directives?|restrictions?|guardrails?|"
    r"programming|constraints?|configuration|system (?:message|prompt)|training|filters?|"
    r"safeguards?|safety (?:rules|guidelines|filters|settings|measures)|content polic(?:y|ies)|"
    r"limitations?|limits|boundaries|commands|policies|persona|role)"
)
_ES_OVERRIDE_VERBS: Final = (
    r"(?:ignora|ignore|ignorar|ignoren|olvida|olvide|olvidar|olvidate de|omite|omitir|descarta|"
    r"descartar|saltate|saltese|pasa por alto|no sigas|no hagas caso (?:a|de)|deja de seguir|"
    r"deja de obedecer|anula|desactiva|haz caso omiso (?:a|de))"
)
_ES_TARGETS: Final = (
    r"(?:instrucciones|reglas|normas|indicaciones|directrices|restricciones|prompt|"
    r"programacion|limites|limitaciones|filtros|politicas|ordenes|configuracion)"
)

OVERRIDE_RULES: Final = _rules(
    "override",
    (
        (
            "ignore_your_instructions",
            rf"\b{_OVERRIDE_VERBS}\b(?:[^\w.!?\n]+\w+){{0,3}}?[^\w.!?\n]+{_OWN_QUALIFIERS}\b"
            rf"(?:[^\w.!?\n]+\w+){{0,2}}?[^\w.!?\n]+{_TARGETS}\b",
        ),
        (
            "ignore_previous_instructions",
            rf"{_CLAUSE}{_OVERRIDE_VERBS}\b(?:[^\w.!?\n]+\w+){{0,3}}?[^\w.!?\n]+{_ANY_QUALIFIERS}\b"
            rf"(?:[^\w.!?\n]+\w+){{0,2}}?[^\w.!?\n]+{_TARGETS}\b",
        ),
        (
            "ignore_everything_above",
            r"\b(?:ignore|disregard|forget)\b[^\w.!?\n]+(?:(?:all|"
            r"everything)[^\w.!?\n]+)?(?:of[^\w.!?\n]+)?(?:the[^\w.!?\n]+)?"
            r"(?:above|previous|prior|before|preceding|what (?:you were|you'?ve been|you have "
            r"been|i) (?:told|said|given)(?: before)?|everything (?:you know|above|before))\b",
        ),
        (
            "disable_safety",
            r"\b(?:disable|turn off|switch off|deactivate|remove|bypass|get around|circumvent|"
            r"lift|drop)\b(?:[^\w.!?\n]+\w+){0,2}?[^\w.!?\n]+(?:your|the|all|"
            r"any)[^\w.!?\n]+(?:\w+[^\w.!?\n]+)?(?:filters?|"
            r"safety|guardrails?|restrictions?|content polic(?:y|ies)|safeguards?|moderation|"
            r"censorship|limitations)\b",
        ),
        (
            "es_ignora_tus_instrucciones",
            rf"\b{_ES_OVERRIDE_VERBS}\b(?:[^\w.!?\n]+\w+){{0,3}}?[^\w.!?\n]+(?:tus|sus|vuestras|"
            rf"tu)\b"
            rf"(?:[^\w.!?\n]+\w+){{0,2}}?[^\w.!?\n]+{_ES_TARGETS}\b",
        ),
        (
            "es_ignora_las_instrucciones_anteriores",
            rf"\b{_ES_OVERRIDE_VERBS}\b(?:[^\w.!?\n]+\w+){{0,2}}?[^\w.!?\n]+(?:las|los|todas las|"
            rf"todos los)[^\w.!?\n]+"
            rf"(?:\w+[^\w.!?\n]+)?{_ES_TARGETS}[^\w.!?\n]+(?:anteriores|previas|previos|originales|"
            rf"iniciales|de "
            r"arriba|del sistema|que te (?:dieron|han dado|di)|que tienes)\b",
        ),
        (
            "es_ignora_todo_lo_anterior",
            rf"\b{_ES_OVERRIDE_VERBS}\b[^\w.!?\n]+(?:todo|todas?)[^\w.!?\n]+(?:lo[^\w.!?\n]+|"
            rf"las[^\w.!?\n]+)?(?:anterior|"
            r"anteriores|de arriba|previo|que te dijeron)\b",
        ),
    ),
)

_NO_RULES: Final = (
    r"(?:rules|restrictions|limits|limitations|filters|guidelines|boundaries|censorship|"
    r"ethics|morals|policies|instructions|guardrails)"
)

PERSONA_RULES: Final = _rules(
    "persona",
    (
        ("you_are_now", r"\b(?:you are|you'?re|u r|ur) (?:now|no longer)\b"),
        (
            "from_now_on_you",
            r"\bfrom (?:now|this moment|today) on,?\s+(?:you|"
            r"u)\b(?:[^\w.!?\n]+\w+){0,2}?[^\w.!?\n]+(?:are|will|"
            r"must|shall|act|respond|answer|reply|behave|speak|be|have|only)\b",
        ),
        ("act_as_if", r"\b(?:act|behave|respond|answer|reply)(?:ing)? as (?:if|though)\b"),
        ("act_as", rf"{_CLAUSE}(?:act|behave|pose|roleplay|role-play|role play)\s+(?:as|like)\b"),
        (
            "answer_as_someone",
            rf"{_CLAUSE}(?:respond|answer|reply|speak|talk|write)\s+(?:as|like)\s+(?:if\s+you|"
            r"though\s+you|an?\s|my\s|someone|somebody)",
        ),
        (
            "assistant_without_rules",
            rf"\b(?:assistant|ai|bot|chatbot|model|llm|gpt|persona|character|version of you|"
            rf"yourself|mode)\b(?:[^\w.!?\n]+\w+){{0,4}}?[^\w.!?\n]+(?:with no|without(?: any)?|"
            rf"with zero|free "
            rf"(?:of|from)|minus|unbound by|not bound by)\s+(?:\w+\s+)?{_NO_RULES}\b",
        ),
        ("you_have_no_rules", rf"\b(?:you|u) (?:have|had|got) no\s+(?:\w+\s+)?{_NO_RULES}\b"),
        ("no_rules_apply", rf"\bno\s+{_NO_RULES} (?:apply|anymore|for you|from now)\b"),
        (
            "pretend",
            r"\bpretend(?:ing)? (?:to be|you(?:'re| are| were)|that you|u r|you have|you had)\b",
        ),
        ("imagine_you_are", r"\bimagine (?:you(?:'re| are| were)|that you(?:'re| are| were))\b"),
        (
            "special_mode",
            r"\b(?:developer|dev|god|admin|debug|maintenance|sudo|root|unrestricted|jailbreak|"
            r"jailbroken|unfiltered|uncensored|evil|dan|chaos|opposite) mode\b",
        ),
        ("do_anything_now", r"\bdo anything now\b|\b(?:you are|you'?re|as|be) dan\b"),
        ("jailbreak", r"\bjailbr(?:ea|o)k\w*\b"),
        ("roleplay", r"\b(?:roleplay|role-play|role play)(?:ing)?\b|\bjuego de rol\b"),
        (
            "change_role",
            r"\b(?:change|switch|swap|update|reset|rewrite|replace) (?:your )?(?:role|persona|"
            r"personality|identity|character|system prompt|core instructions)\b",
        ),
        ("new_persona", r"\bnew (?:role|persona|identity|personality|character)\b"),
        ("simulate", r"\bsimulate (?:being|an? (?:unrestricted|unfiltered|different|other))\b"),
        (
            "es_a_partir_de_ahora",
            r"\b(?:a partir de (?:ahora|este momento|hoy)|desde ahora|de ahora en adelante),?\s+"
            r"(?:tu\s+)?(?:eres|seras|vas a|actua|actuaras|responde|responderas|te llamas|"
            r"ya no|no tienes|olvida)\b",
        ),
        ("es_ahora_eres", r"\b(?:ahora|ya no) eres\b"),
        (
            "es_actua_como",
            r"(?:^|[.!?;:,\"(]\s*|\b(?:por favor|ahora|y|puedes|podrias|quiero que|necesito "
            r"que)\s+)(?:actua|actues|comportate|te comportes|responde|respondas|habla|hables|"
            r"finge|finjas|hazte pasar)\s+(?:como|por)\b",
        ),
        ("es_como_si", r"\b(?:actua|responde|haz|habla|comportate) como si\b"),
        (
            "es_sin_reglas",
            r"\b(?:asistente|ia|bot|chatbot|modelo|version|tu|ti|"
            r"modo)\b(?:[^\w.!?\n]+\w+){0,4}?[^\w.!?\n]+"
            r"sin\s+(?:ningun[ao]?s?\s+)?(?:reglas|restricciones|limites|filtros|normas|"
            r"censura|instrucciones|politicas)\b",
        ),
        (
            "es_no_tuvieras_reglas",
            r"\bno (?:tuvieras|tienes|tengas|tendras) (?:\w+\s+)?(?:reglas|restricciones|"
            r"limites|filtros|normas|instrucciones)\b",
        ),
        ("es_finge", r"\b(?:finge|imagina) (?:ser|que eres|que no)\b"),
        (
            "es_modo",
            r"\bmodo (?:desarrollador|dios|sin restricciones|sin filtros|sin reglas|"
            r"administrador|depuracion|libre|dan)\b",
        ),
    ),
)

IDENTITY_RULES: Final = _rules(
    "identity",
    (
        (
            "forget_trackflow",
            r"(?<!n't )(?<!not )(?<!never )\bforget\b(?:[^\w.!?\n]+\w+){0,3}?[^\w.!?\n]+"
            r"(?:trackflow|track flow|the company|"
            r"your (?:company|employer|job|role|purpose|identity|owners?|creators?|mission)|"
            r"who you (?:are|work for)|that you (?:are|work)|you (?:are|work) (?:for|at))\b",
        ),
        (
            "you_dont_work_for_trackflow",
            r"\byou (?:do not|don'?t|no longer|never) (?:work|belong) (?:for|at|with|to) "
            r"(?:trackflow|track flow|the company|them|us)\b",
        ),
        (
            "stop_being_trackflow",
            r"\b(?:stop|quit) (?:being|acting as|working for|representing|pretending to be) "
            r"(?:trackflow|a trackflow|the trackflow|their|an? (?:support|cx|customer))",
        ),
        (
            "not_trackflow_assistant",
            r"\byou(?:'re| are) (?:not|no longer) (?:trackflow'?s?|a trackflow|the trackflow|their|"
            r"an? (?:support|cx|customer service)) (?:assistant|agent|bot)\b",
        ),
        (
            "es_olvida_trackflow",
            r"\bolvid(?:a|ate|e|ar|ad)\b(?:[^\w.!?\n]+\w+){0,3}?[^\w.!?\n]+(?:trackflow|track flow|"
            r"la empresa|"
            r"tu (?:empresa|trabajo|rol|proposito|identidad)|quien eres|que trabajas|que eres)\b",
        ),
        (
            "es_ya_no_trabajas",
            r"\b(?:ya no|no) (?:trabajas|perteneces) (?:para|en|a) (?:trackflow|la empresa)\b",
        ),
        (
            "es_deja_de_ser",
            r"\bdeja de (?:ser|trabajar para|actuar como|representar a) (?:trackflow|un|una|el|"
            r"la|asistente|agente)\b",
        ),
    ),
)

_PROMPT_NOUNS: Final = (
    r"(?:system prompt|system message|prompt|instructions|rules|guidelines|directives|"
    r"configuration|programming|guardrails|initial message|hidden message|meta ?prompt)"
)
# "your instructions for a lost parcel" is a question about a procedure.
_NOT_A_PROCEDURE: Final = (
    r"(?!\s+(?:for|about|on|to|regarding|when|if|in case|with|of|around|concerning)\b)"
)

EXTRACTION_RULES: Final = _rules(
    "extraction",
    (
        (
            "show_your_prompt",
            r"\b(?:show|reveal|print|repeat|display|output|tell|give|share|list|dump|leak|expose|"
            r"recite|paste|copy|spell out|write out|send|summari[sz]e|translate|explain)\b"
            rf"(?:[^\w.!?\n]+\w+){{0,3}}?[^\w.!?\n]+(?:your|"
            rf"ur)\s+(?:\w+\s+){{0,2}}?{_PROMPT_NOUNS}\b"
            rf"{_NOT_A_PROCEDURE}",
        ),
        (
            "what_are_your_instructions",
            rf"\bwhat (?:are|were|is) (?:your|ur)\s+(?:\w+\s+){{0,2}}?{_PROMPT_NOUNS}\b"
            rf"{_NOT_A_PROCEDURE}",
        ),
        (
            "system_prompt",
            r"\b(?:system prompt|hidden prompt|initial prompt|meta ?prompt|prompt injection|"
            r"developer message)\b",
        ),
        (
            "repeat_above",
            r"\b(?:repeat|print|output|show|reveal|copy|"
            r"recite)\b(?:[^\w.!?\n]+\w+){0,4}?[^\w.!?\n]+(?:above|"
            r"before this|preceding|verbatim|word for word|everything you (?:were|have been) "
            r"(?:told|given))\b",
        ),
        (
            "how_were_you_instructed",
            r"\b(?:what (?:were you|have you been) (?:told|instructed|programmed|trained) "
            r"(?:to|with)|how (?:were|are) you (?:programmed|instructed|configured|prompted))\b",
        ),
        (
            "es_muestra_tus_instrucciones",
            r"\b(?:muestra(?:me)?|ensena(?:me)?|revela(?:me)?|imprime|repite|dime|di|comparte|"
            r"escribe|copia|lista|cuales son)\b(?:[^\w.!?\n]+\w+){0,3}?[^\w.!?\n]+(?:tus|"
            r"tu)\s+(?:\w+\s+){0,2}?"
            r"(?:instrucciones|prompt|reglas|indicaciones|directrices|configuracion|"
            r"programacion)\b(?!\s+(?:para|sobre|de|en caso|acerca|con)\b)",
        ),
        ("es_mensaje_del_sistema", r"\b(?:el )?(?:prompt|mensaje) (?:del|de) sistema\b"),
    ),
)

AUTHORITY_RULES: Final = _rules(
    "authority",
    (
        ("chat_markup", r"<\|\s*(?:im_start|im_end|system|endoftext|assistant|user)\s*\|>"),
        ("inst_tags", r"\[/?\s*(?:inst|sys|system)\s*\]|<<\s*/?\s*(?:sys|untrusted)\b"),
        ("role_tags", r"</?\s*(?:system|assistant|instructions?|admin|developer)\s*>"),
        (
            "role_header",
            r"^\s*(?:#{1,6}\s*)?(?:system|assistant|developer|admin|sys)\s*(?:prompt|message|"
            r"note|override|instruction)?\s*:",
        ),
        ("markdown_header", r"^\s*#{1,6}\s*(?:system|instructions?|new instructions)\b"),
        ("new_instructions", r"\bnew (?:system )?(?:instructions|directives|prompt)\b"),
        (
            "message_from_authority",
            r"\b(?:message|instructions?|orders?|update|note|directive|command) from (?:the |your "
            r")?(?:developers?|devs?|admins?|administrators?|system|openai|anthropic|cto|ceo|"
            r"creators?|owners?|trackflow (?:it|tech|security|admin|management|developers?))\b",
        ),
        (
            "authority_allows",
            r"\b(?:the |your |my )?(?:developers?|admins?|administrators?|cto|ceo|creators?|"
            r"owners?|supervisors?|security team)\b(?:[^\w.!?\n]+\w+){0,2}?[^\w.!?\n]+(?:says?|"
            r"said|has "
            r"(?:authori[sz]ed|approved|allowed)|authori[sz]es|approves|allows?|wants|told me|"
            r"lets) (?:you|that you|me to|it)\b",
        ),
        (
            "i_am_your_developer",
            r"\bi(?:'m| am) (?:your|the|a|an) (?:developer|admin|administrator|creator|owner|"
            r"programmer|system|trackflow (?:developer|admin|engineer))\b",
        ),
        (
            "privileged_access",
            r"\b(?:admin|sudo|override|maintenance|debug|developer|root) (?:access|command|code|"
            r"password|privileges?|override|clearance|token)\b|\bauthori[sz]ed to (?:override|"
            r"bypass|ignore|change)\b",
        ),
        (
            "es_nuevas_instrucciones",
            r"\bnuevas (?:instrucciones|directrices|ordenes del sistema)\b",
        ),
        (
            "es_soy_tu_desarrollador",
            r"\b(?:soy|habla) (?:tu|el|un) (?:desarrollador|administrador|creador|programador|"
            r"dueno)\b",
        ),
        (
            "es_autoridad_permite",
            r"\b(?:el|mi|tu) (?:administrador|desarrollador|cto|ceo|creador|jefe|supervisor)\b"
            r"(?:[^\w.!?\n]+\w+){0,2}?[^\w.!?\n]+(?:dice|dijo|ha autorizado|autoriza|permite|"
            r"quiere) (?:que|te)\b",
        ),
    ),
)

INSTRUCTION_CHANGE_RULES: Final[tuple[Rule, ...]] = (
    *OVERRIDE_RULES,
    *PERSONA_RULES,
    *IDENTITY_RULES,
    *EXTRACTION_RULES,
    *AUTHORITY_RULES,
)

# --- Content: personal-chatbot use ----------------------------------------------------

_CREATIVE: Final = (
    r"(?:essays?|poems?|poetry|haiku|sonnet|limerick|short story|fairy tale|bedtime story|"
    r"novel|songs?|lyrics|rap|love letter|cover letter|speech|eulogy|toast|screenplay|"
    r"fan ?fiction|birthday (?:message|card|poem))"
)

PERSONAL_TASK_RULES: Final = _rules(
    "personal",
    (
        (
            "creative_writing",
            r"\b(?:write|compose|draft|craft|pen)\b(?:[^\w.!?\n]+\w+){0,4}?[^\w.!?\n]+"
            rf"(?:{_CREATIVE}|stor(?:y|ies)|blog post|article|tweet|instagram (?:post|caption)|"
            r"caption|cv|curriculum)\b",
        ),
        (
            "creative_request",
            r"\b(?:create|generate|make|produce|give me|help me with|do|need|want)\b"
            rf"(?:[^\w.!?\n]+\w+){{0,4}}?[^\w.!?\n]+{_CREATIVE}\b",
        ),
        (
            "homework",
            r"\b(?:homework|home work|coursework|course work|thesis|dissertation|term paper|"
            r"book report|school project|science project|exam questions?|quiz questions|"
            r"study guide|(?:university|college|school|class|history|math|maths|english) "
            r"assignment|my (?:exam|midterm|professor|teacher|lecture|homework|assignment))\b",
        ),
        (
            "help_with_studies",
            r"\bhelp (?:me )?with my (?:homework|assignment|essay|exam|thesis|studies|coursework|"
            r"project for (?:school|class|university))\b",
        ),
        (
            "write_code",
            r"\b(?:write|code|debug|fix|refactor|optimi[sz]e|review|complete|generate|create|"
            r"build|implement|program|develop)\b(?:[^\w.!?\n]+\w+){0,4}?[^\w.!?\n]+(?:(?:some|the|"
            r"this|my|"
            r"a|python|java\w*|c\+\+|source) code|scripts?|functions?|algorithms?|"
            r"regex|regular expressions?|sql quer(?:y|ies)|snippets?|unit tests?|web ?page|"
            r"website|html page|chrome extension|excel formula|vba macro|leetcode)\b",
        ),
        (
            "programming_language",
            r"\b(?:in|using|with) (?:python|javascript|typescript|java|c\+\+|c#|golang|rust|"
            r"ruby|php|kotlin|swift|bash|powershell|html|css|react|node(?:\.js)?)\b|\b(?:python|"
            r"javascript|typescript|java|golang|rust|ruby|php|kotlin|swift|bash|powershell|sql|"
            r"html|css|react)\s+(?:code|script|function|program|snippet|query|class|app|bug|"
            r"error)\b",
        ),
        ("code_error", r"\b(?:stack ?trace|syntax error|compil(?:e|er|ation) error|traceback)\b"),
        (
            "be_my",
            r"\b(?:be|act as|become|you(?:'re| are) now) my (?:therapist|psychologist|"
            r"psychiatrist|counsell?or|life coach|coach|doctor|lawyer|attorney|friend|best friend|"
            r"girlfriend|boyfriend|partner|wife|husband|tutor|teacher|mentor|financial advisor)\b",
        ),
        (
            "personal_advice",
            r"\b(?:relationship|dating|love|romantic|marriage|breakup|break-up|divorce|parenting|"
            r"diet|dieting|weight[- ]loss|nutrition|workout|fitness|career|investment|investing|"
            r"stock market|crypto|financial|tax|legal|medical|health|mental health)\s+(?:advice|"
            r"tips|guidance|counsel(?:l?ing)?)\b",
        ),
        (
            "personal_life",
            r"\bmy (?:girlfriend|boyfriend|wife|husband|partner|ex|crush|boss|mom|mother|dad|"
            r"father) (?:and i|doesn'?t|does not|left|broke|cheated|is mad|hates)\b|\bshould i "
            r"(?:break up|divorce|quit my job|invest|buy (?:bitcoin|crypto|stocks))\b",
        ),
        (
            "everyday_tasks",
            r"\b(?:recipe for|how (?:do|to) (?:i )?(?:cook|bake)|plan (?:my|a) (?:trip|vacation|"
            r"holiday|wedding|party|diet|workout))\b",
        ),
        (
            "maths",
            r"\b(?:solve|calculate|integrate|"
            r"differentiate)\b(?:[^\w.!?\n]+\w+){0,3}?[^\w.!?\n]+(?:equations?|"
            r"integrals?|derivatives?|math problems?|maths problems?|algebra|calculus)\b",
        ),
        (
            "es_escribe_un_ensayo",
            r"\b(?:escribe|escribeme|escribir|redacta|redactame|hazme|crea|creame|genera|generame|"
            r"componme|compon)\b(?:[^\w.!?\n]+\w+){0,4}?[^\w.!?\n]+(?:ensayo|redaccion|poema|"
            r"poesia|cuento|relato|"
            r"novela|cancion|carta de amor|discurso|curriculum|codigo|script|funcion|algoritmo|"
            r"tarea|deberes|trabajo (?:de|para) (?:clase|la universidad|el colegio|la escuela))\b",
        ),
        (
            "es_mi_tarea",
            r"\b(?:ayudame|ayuda(?:me)?|puedes ayudarme|hazme) (?:a hacer |con )?(?:mi|mis|la) "
            r"(?:tarea|tareas|deberes|examen|tesis|trabajo de clase|ensayo|redaccion)\b|\b(?:tarea|"
            r"deberes|tesis|examen) (?:de|para) (?:la universidad|el colegio|la escuela|clase|"
            r"historia|matematicas)\b",
        ),
        (
            "es_se_mi",
            r"\bse (?:mi|como mi) (?:terapeuta|psicologo|psicologa|novio|novia|amigo|amiga|pareja|"
            r"profesor|abogado|medico)\b",
        ),
        (
            "es_consejos",
            r"\bconsejos? (?:amorosos?|de pareja|sentimentales?|de dieta|de citas|para ligar|"
            r"financieros?|de inversion)\b",
        ),
        (
            "es_programacion",
            r"\b(?:en|con) (?:python|javascript|java|c\+\+|sql|html|php)\b",
        ),
    ),
)

# --- Content: allowed with the redirect -------------------------------------------------

# A message naming TrackFlow's domain is not small talk, whatever else it says.
_DOMAIN_WORDS: Final = re.compile(
    r"\b(?:orders?|tracking|track|shipments?|shipping|ship|parcels?|packages?|deliver\w*|"
    r"returns?|returned|refunds?|carriers?|couriers?|incidents?|tickets?|warehouses?|stock|"
    r"inventory|sla|polic(?:y|ies)|trackflow|track flow|ups|fedex|dhl|mrw|seur|estafeta|customs|"
    r"labels?|pickup|pick-up|address|lost|damaged|pedidos?|envios?|paquetes?|entregas?|"
    r"devolucion\w*|reembolsos?|transportistas?|incidencias?|almacen\w*|seguimiento|aduanas?|"
    r"direccion)\b",
    _FLAGS,
)

SMALL_TALK_RULES: Final = _rules(
    "small_talk",
    (
        (
            "greeting",
            r"\A(?:hi|hello|hey|hiya|howdy|yo|good (?:morning|afternoon|evening|day|night)|"
            r"greetings|hola|buenas|buenos dias|buenas tardes|buenas noches|que tal|saludos)\b"
            r"(?:[\s,!.]+(?:there|team|everyone|all|trackflow|friend|amigo|equipo))?[\s!.,?]*\Z",
        ),
        (
            "thanks_or_goodbye",
            r"\A(?:(?:thanks|thank you|thx|ty|cheers|great|perfect|awesome|nice|cool|ok|okay|"
            r"alright|got it|bye|goodbye|see you|see ya|gracias|muchas gracias|adios|hasta luego|"
            r"perfecto|vale|genial)\b[\s,!.]*)+(?:(?:that'?s|that is) (?:all|resolved|it|great|"
            r"helpful|perfect)|for (?:your|the) help|so much|a lot|have a (?:nice|good|great) "
            r"(?:day|one|weekend))?[\s,!.]*\Z",
        ),
        (
            "wellbeing",
            r"\b(?:how are you|how'?s it going|how are things|how do you do|what'?s up|como estas|"
            r"que tal estas|como te va)\b",
        ),
        (
            "who_are_you",
            r"\b(?:who are you|what are you|what'?s your name|what is your name|are you (?:a )?"
            r"(?:human|robot|bot|real person|real|an ai|ai|chatgpt)|quien eres|que eres|como te "
            r"llamas|eres (?:un |una )?(?:humano|robot|bot|ia))\b",
        ),
        (
            "time_or_date",
            r"\b(?:what(?:'s| is) the time|what time is it|what'?s the date|what(?:'s|"
            r" is) today'?s "
            r"date|what day is (?:it|today)|que hora es|que dia es hoy|a que fecha estamos)\b",
        ),
        (
            "weather",
            r"\b(?:what'?s the weather|how'?s the weather|what is the weather|weather (?:like )?"
            r"(?:in|today|tomorrow|this week)|weather forecast|is it (?:raining|sunny|cold|hot) "
            r"(?:in|today)|que tiempo hace|el clima en|el tiempo en)\b",
        ),
        (
            "joke",
            r"\b(?:tell me a joke|a joke|another joke|make me laugh|something funny|cuentame un "
            r"chiste|un chiste)\b",
        ),
        (
            "trivia",
            r"\b(?:who (?:won|invented|discovered|wrote|painted|directed|is the (?:president|"
            r"king|queen|prime minister))|world cup|super bowl|champions league|olympics|capital "
            r"(?:city )?of|what is the (?:population|tallest|largest|biggest|smallest|longest)|"
            r"how (?:tall|old|far) is|quien gano|capital de|mundial de futbol)\b",
        ),
    ),
)

_GENERAL_TERMS: Final = (
    r"(?:reverse logistics|last[- ]mile(?: delivery)?|first[- ]mile|middle[- ]mile|"
    r"cross[- ]?docking|3pl|third[- ]party logistics|4pl|fourth[- ]party logistics|"
    r"fulfil?ment(?: centers?| centres?)?|incoterms?|drop ?shipping|freight forward(?:ing|er)|"
    r"pick(?:ing)? and pack(?:ing)?|supply chains?|logistics|an sla|sla|service level "
    r"agreements?|bills? of lading|customs clearance|dimensional weight|volumetric weight|"
    r"warehousing|wms|warehouse management systems?|tms|inventory turnover|safety stock|"
    r"logistica inversa|ultima milla|cadena de suministro|almacenaje|logistica)"
)

GENERAL_LOGISTICS_RULES: Final = _rules(
    "general_logistics",
    (
        (
            "definition",
            r"\b(?:what(?:'s| is| are| does)|define|definition of|meaning of|explain|tell me about|"
            r"how does|how do|que es|que son|que significa|define|explica(?:me)?)\b"
            rf"(?:[^\w.!?\n]+\w+){{0,3}}?[^\w.!?\n]+{_GENERAL_TERMS}\b",
        ),
    ),
)
# A general question is about the concept, not about TrackFlow or someone's order.
_ABOUT_TRACKFLOW: Final = re.compile(
    r"\b(?:trackflow|track flow|your|you|my|our|we|us|mi|mis|tu|tus|nuestro|nuestra)\b", _FLAGS
)

# --- Orders -------------------------------------------------------------------------------

_ORDER_KEYWORD: Final = (
    r"(?:order|tracking(?: number| code| no\.?| id)?|track(?:ing)? #|shipment|parcel|package|"
    r"consignment|waybill|awb|pedido|envio|env[ií]o|paquete|seguimiento|n[uú]mero de seguimiento|"
    r"gu[ií]a)"
)
_ORDER_REFERENCE: Final = re.compile(
    rf"\b{_ORDER_KEYWORD}\s*(?:number|no\.?|num(?:ber)?\.?|n[º°o]\.?|#|id|code|c[oó]digo|"
    r"ref(?:erence)?)?\s*(?:is|es|:|#)?\s*#?\s*(?P<id>(?=[A-Z0-9-]*\d)[A-Z0-9][A-Z0-9-]{3,24})\b"
    r"(?!\s*(?:units?|items?|pieces?|pcs|kg|kgs|lbs?|boxes|cubic|m3|days?|hours?|eur|usd|"
    r"euros?|dollars?|%))",
    re.IGNORECASE,
)
# Carrier tracking shapes, without a keyword: a 12-character code of capitals and
# digits (as in TrackFlow's records), UPS's 1Z..., and long numeric codes.
_BARE_TRACKING: Final = (
    re.compile(r"\b(?=[A-Z0-9]{12}\b)(?=[A-Z0-9]*\d)(?=[A-Z0-9]*[A-Z])[A-Z0-9]{12}\b"),
    re.compile(r"\b1Z[0-9A-Z]{16}\b"),
)
_LONG_NUMBER: Final = re.compile(r"\b\d{10,22}\b")
_TICKET_REFERENCE: Final = re.compile(r"^TRF-?\d+$", re.IGNORECASE)
# The customer means their own order(s) without giving a number.
_OWN_ORDER: Final = re.compile(
    r"\b(?:my|mine|our|mi|mis|nuestro|nuestros)\s+(?:\w+\s+)?(?:orders?|parcels?|packages?|"
    r"shipments?|deliver(?:y|ies)|returns?|purchases?|pedidos?|paquetes?|envios?|entregas?|"
    r"devoluciones|devolucion|compras?)\b",
    _FLAGS,
)


def extract_order_references(text: str) -> list[str]:
    """The order or tracking numbers a text names, upper-cased, in order, once each.

    Helpdesk ticket references (TRF-...) are tickets, not orders. A long number
    only counts when the text speaks of orders or tracking.
    """
    shown = visible(text)
    found: list[str] = []

    def add(candidate: str) -> None:
        identifier = candidate.strip("-").upper()
        if identifier and not _TICKET_REFERENCE.match(identifier) and identifier not in found:
            found.append(identifier)

    for match in _ORDER_REFERENCE.finditer(shown):
        add(match.group("id"))
    for pattern in _BARE_TRACKING:
        for match in pattern.finditer(shown):
            add(match.group(0))
    if _ORDER_REFERENCE.search(shown) or re.search(rf"\b{_ORDER_KEYWORD}\b", shown, re.I):
        for match in _LONG_NUMBER.finditer(shown):
            add(match.group(0))
    return found


# --- Country policies -----------------------------------------------------------------

_COUNTRY_WORDS: Final[tuple[tuple[str, str], ...]] = (
    ("ES", r"spain|spanish|espana|espanola|espanol|zaragoza|madrid|barcelona|aragon"),
    (
        "US",
        r"the us|the u\.s\.?|u\.s\.a?\.?|usa|american|america|united states|estados unidos|"
        r"eeuu|ee\.uu\.|los angeles|california",
    ),
)
_POLICY_WORDS: Final = (
    r"(?:return|returns|refund|delivery|shipping|sla|devolucion|devoluciones|reembolso|entrega|"
    r"envio)?\s*(?:polic(?:y|ies)|rules?|terms|conditions|sla|window|politicas?|normas?|"
    r"condiciones|plazo)"
)
_APPLY_INTENT: Final = re.compile(
    r"\b(?:apply|use|give me|switch|change|swap|treat|honou?r|grant|follow|instead|rather "
    r"than|want|prefer|aplica|aplicame|aplicar|aplique|usa|usar|utiliza|cambia|cambiar|en vez "
    r"de|en lugar de|trata|sigue|quiero|prefiero)\b",
    _FLAGS,
)
_BENEFIT: Final = re.compile(
    r"\b(?:benefits? me|better for me|more favou?rable|cheaper|longer|more generous|suits me|"
    r"me conviene|me beneficia|mas favorable|mejor para mi)\b",
    _FLAGS,
)
_ORDER_CONTEXT: Final = re.compile(
    r"\b(?:my|this|the|that|mi|este|esta|el|la)\s+(?:\w+\s+)?(?:order|parcel|package|shipment|"
    r"return|purchase|delivery|pedido|paquete|envio|devolucion|compra|entrega)\b",
    _FLAGS,
)
_PLACE_OF_ORDER: Final = re.compile(
    r"\b(?:order|parcel|package|shipment|purchase|delivery|return|pedido|paquete|envio|compra|"
    r"entrega|devolucion)\b[^.!?\n]{0,40}?\b(?:in|from|to|at|en|de|desde|a|hacia)\s+"
    r"(?:the\s+)?(?P<place>los angeles|california|the us|usa|united states|america|"
    r"estados unidos|eeuu|spain|espana|zaragoza|madrid|barcelona|aragon)\b",
    _FLAGS,
)


def _country_of_word(word: str) -> str | None:
    for country, words in _COUNTRY_WORDS:
        if re.fullmatch(rf"(?:{words})", word.strip(), re.IGNORECASE):
            return country
    return None


# "US" written in capitals, never the pronoun "us": checked on the original text.
_US_IN_CAPITALS: Final = re.compile(
    r"\bUS(?:'s)?\s+(?:\w+\s+){0,2}?(?:return|returns|refund|delivery|shipping|SLA|policy|"
    r"policies|rules|terms|conditions|window)\b|\b(?:policy|policies|rules|terms|SLA|window) "
    r"(?:of|from|for|in) the US\b"
)


def _requested_policy_country(folded: str, original: str = "") -> str | None:
    """The country whose policy the message asks for ("Spain's return policy")."""
    if _US_IN_CAPITALS.search(visible(original)):
        return "US"
    for country, words in _COUNTRY_WORDS:
        before = re.search(rf"\b(?:{words})(?:'s|s)?\s+(?:\w+\s+){{0,2}}?{_POLICY_WORDS}\b", folded)
        after = re.search(
            rf"\b{_POLICY_WORDS}\s+(?:\w+\s+){{0,2}}?(?:of|from|for|in|de|del|en)\s+(?:the\s+)?"
            rf"(?:{words})\b",
            folded,
        )
        if before or after:
            return country
    return None


# --- The verdict ------------------------------------------------------------------------


@dataclass(frozen=True)
class InputVerdict:
    action: Action
    category: Category
    failure_type: FailureType | None = None
    # The rule that decided, and every rule that matched (for the log).
    rule: str | None = None
    rules: tuple[str, ...] = ()
    # Order numbers the message names; those the customer may not see.
    references: tuple[str, ...] = ()
    unauthorized: tuple[str, ...] = ()
    # The message speaks of the customer's own order(s) without a number.
    own_orders: bool = False
    # The country whose terms apply, and the one the message asked for instead.
    policy_country: str | None = None
    requested_country: str | None = None

    @property
    def stops_the_run(self) -> bool:
        return self.action in ("block", "refuse")

    def to_state(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "category": self.category,
            "failure_type": self.failure_type,
            "rule": self.rule,
            "rules": list(self.rules),
            "references": list(self.references),
            "unauthorized": list(self.unauthorized),
            "own_orders": self.own_orders,
            "policy_country": self.policy_country,
            "requested_country": self.requested_country,
        }

    def events(self) -> list[dict[str, Any]]:
        """The guardrail events this verdict is: none for a plain `allow`."""
        guardrail = {
            "instruction_change": "injection_filter",
            "personal_task": "scope_filter",
            "small_talk": "scope_filter",
            "general_logistics": "scope_filter",
            "unauthorized_order": "order_authorization",
            "policy_mixing": "policy_country",
            "malformed": "input_structure",
        }.get(self.category)
        if self.action == "allow" or guardrail is None:
            return []
        return [
            {
                "guardrail": guardrail,
                "action": self.action,
                "failure_type": self.failure_type or "content",
                "rule": self.rule or self.category,
                "source": "customer_message",
            }
        ]


def _matches(rules: Iterable[Rule], *texts: str) -> list[str]:
    return [rule.id for rule in rules if any(rule.pattern.search(text) for text in texts)]


def find_instruction_change(text: str) -> list[str]:
    """The ids of every instruction-change rule `text` matches (any language)."""
    return _matches(INSTRUCTION_CHANGE_RULES, fold(text), fold(text, leet=False))


def find_personal_task(text: str) -> list[str]:
    return _matches(PERSONAL_TASK_RULES, fold(text), fold(text, leet=False))


def _small_talk(plain: str) -> list[str]:
    if _DOMAIN_WORDS.search(plain):
        return []
    return _matches(SMALL_TALK_RULES, plain)


def _general_logistics(plain: str) -> list[str]:
    if _ABOUT_TRACKFLOW.search(plain):
        return []
    return _matches(GENERAL_LOGISTICS_RULES, plain)


def _policy_country(
    plain: str,
    references: list[str],
    owned: Mapping[str, str],
    country_of: Callable[[str], str | None],
) -> str | None:
    """The country whose terms apply: the records first, then the place named for the order."""
    recorded = {country_of(reference) for reference in references} - {None}
    if not recorded and not references and owned:
        recorded = set(owned.values())
    if len(recorded) == 1:
        return next(iter(recorded))
    if recorded:
        return None  # orders in both countries: ask
    place = _PLACE_OF_ORDER.search(plain)
    return _country_of_word(place.group("place")) if place else None


def inspect_message(
    text: str,
    *,
    audience: str = "staff",
    owned: Mapping[str, str] | None = None,
    country_of: Callable[[str], str | None] | None = None,
) -> InputVerdict:
    """The guard's verdict on one message.

    `owned` maps the customer's own tracking numbers to their country; `country_of`
    gives the country of any order in TrackFlow's records (for staff, and for the
    customer's own orders). Neither is needed for the rules on instructions or scope.
    """
    owned = {key.upper(): value for key, value in (owned or {}).items()}
    lookup = country_of or (lambda reference: owned.get(reference.upper()))

    if not visible(text).strip():
        return InputVerdict("refuse", "malformed", "structural", "structure.unreadable")
    if len(text) > MAX_MESSAGE_CHARS:
        return InputVerdict("refuse", "malformed", "structural", "structure.too_long")

    folded = fold(text)
    plain = fold(text, leet=False)

    attacks = _matches(INSTRUCTION_CHANGE_RULES, folded, plain)
    personal = _matches(PERSONAL_TASK_RULES, folded, plain)
    if attacks:
        return InputVerdict(
            "block",
            "instruction_change",
            "security",
            attacks[0],
            tuple(attacks + personal),
        )

    references = extract_order_references(text)
    customer = audience == "customer"
    unauthorized = tuple(ref for ref in references if customer and ref not in owned)
    if unauthorized:
        return InputVerdict(
            "refuse",
            "unauthorized_order",
            "content",
            "order.not_owned_by_session",
            ("order.not_owned_by_session",),
            tuple(references),
            unauthorized,
        )
    if personal:
        return InputVerdict(
            "refuse", "personal_task", "content", personal[0], tuple(personal), tuple(references)
        )

    own_orders = bool(_OWN_ORDER.search(plain)) and not references
    requested = _requested_policy_country(plain, text)
    if (
        requested
        and _APPLY_INTENT.search(plain)
        and (_ORDER_CONTEXT.search(plain) or _BENEFIT.search(plain) or references)
    ):
        actual = _policy_country(plain, references, owned if customer else {}, lookup)
        if actual != requested:
            rule = f"policy.{requested.lower()}_for_{(actual or 'unknown').lower()}_order"
            return InputVerdict(
                "constrain",
                "policy_mixing",
                "content",
                rule,
                (rule,),
                tuple(references),
                own_orders=own_orders,
                policy_country=actual,
                requested_country=requested,
            )

    country = _policy_country(plain, references, owned if customer else {}, lookup)
    small_talk = _small_talk(plain)
    if small_talk:
        return InputVerdict("redirect", "small_talk", "content", small_talk[0], tuple(small_talk))
    general = _general_logistics(plain)
    if general:
        return InputVerdict("redirect", "general_logistics", "content", general[0], tuple(general))
    return InputVerdict(
        "allow",
        "in_domain",
        references=tuple(references),
        own_orders=own_orders,
        policy_country=country,
    )


def rules_fingerprint() -> list[list[str]]:
    """Every rule id and pattern, for the graph fingerprint."""
    families = (
        INSTRUCTION_CHANGE_RULES,
        PERSONAL_TASK_RULES,
        SMALL_TALK_RULES,
        GENERAL_LOGISTICS_RULES,
    )
    rules = [[rule.id, rule.pattern.pattern] for family in families for rule in family]
    others = [
        _ORDER_REFERENCE.pattern,
        *(p.pattern for p in _BARE_TRACKING),
        _DOMAIN_WORDS.pattern,
        _APPLY_INTENT.pattern,
        _PLACE_OF_ORDER.pattern,
    ]
    return rules + [["orders_and_policy", pattern] for pattern in others]
