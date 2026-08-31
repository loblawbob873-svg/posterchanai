"""THE TOOL PATH HAS REGRESSED TWICE AND WAS PARSED BY NOTHING UNDER TEST.

`tool_calling.py` is 599 lines with ZERO test references, and `parse_tool_calls` is the funnel every
agentic turn goes through: text in, `(content, tool_calls)` out. It recognises FIVE different
emission formats because five different models emit five different things, and each format was added
after a model silently stopped working.

WHY A REGRESSION HERE IS INVISIBLE. A tool call that fails to parse is not an error — it is
returned as `content`. The agent renders it as prose and does nothing, the model sees no tool
result, and the turn ends looking like the model chose to answer instead of act. That is precisely
how the nested-tag form was found:

    Without this the block isn't recognized as a call and falls through as prose -> the agent
    (opencode) renders it as text and silently does nothing.

Two of the five branches exist because of a shipped regression (the nested-tag form, and the
markdown-fenced form for OpenAI-style instruct models). Nothing was stopping a third.

Every expectation below was MEASURED against the shipped parser first, not derived from reading it
— including the two places where reading it gives the wrong answer:

  * `<tool_call>{...}</tool_call>` uses a NON-GREEDY `\\{.*?\\}`, which on its own would stop at the
    first `}` and break every call with nested arguments. What makes it safe is the trailing
    `</tool_call>` anchor. Nested arguments are the common case, so that anchor is load-bearing.
  * A fenced block that is NOT a call suppresses the bare-JSON fallback entirely — deliberate, so a
    JSON example in prose is not executed.
"""
import json

import pytest

from app.services import tool_calling as tc


def _names_and_args(calls):
    return [(c["function"]["name"], json.loads(c["function"]["arguments"])
             if c["function"]["arguments"].startswith(("{", "[")) else c["function"]["arguments"])
            for c in calls]


# --------------------------------------------------------------------------- the five formats
#
# One test per format. They are not interchangeable: each is the only thing some deployed model
# emits, so losing one silences that model completely while every other model keeps working —
# which is why these regressions get reported as "the new model doesn't do anything".


def test_format_1_json_hermes():
    content, calls = tc.parse_tool_calls(
        '<tool_call>{"name":"bash","arguments":{"command":"ls"}}</tool_call>')
    assert _names_and_args(calls) == [("bash", {"command": "ls"})]
    assert content is None


def test_format_1b_nested_tags():
    """`project_nested_tag_tool_parse` — the branch added after an agent silently did nothing."""
    content, calls = tc.parse_tool_calls(
        "<tool_call><tool>bash</tool><input>command=ls -la</input></tool_call>")
    assert _names_and_args(calls) == [("bash", {"command": "ls -la"})]
    assert content is None


@pytest.mark.parametrize("name_tag", ["tool", "tool_name", "name", "function"])
@pytest.mark.parametrize("args_tag", ["input", "arguments", "args", "parameters"])
def test_format_1b_accepts_every_spelling_it_claims_to(name_tag, args_tag):
    """The regex advertises four name tags and four args tags. A model emits ONE of them, so a
    spelling quietly dropped from the alternation breaks that model and nothing else."""
    text = (f"<tool_call><{name_tag}>read</{name_tag}>"
            f"<{args_tag}>path=/tmp/x</{args_tag}></tool_call>")
    _content, calls = tc.parse_tool_calls(text)
    assert _names_and_args(calls) == [("read", {"path": "/tmp/x"})]


def test_format_2_qwen_native():
    content, calls = tc.parse_tool_calls(
        "<function=read><parameter=path>/tmp/x</parameter></function>")
    assert _names_and_args(calls) == [("read", {"path": "/tmp/x"})]
    assert content is None


def test_format_3_function_calls_wrapper():
    content, calls = tc.parse_tool_calls(
        '<function-calls>{"name":"glob","arguments":{"pattern":"*.py"}}</function-calls>')
    assert _names_and_args(calls) == [("glob", {"pattern": "*.py"})]
    assert content is None


@pytest.mark.parametrize("tag", ["function-calls", "function_calls", "function-call",
                                 "function_call"])
def test_format_3_tolerates_the_singular_and_underscore_spellings(tag):
    _content, calls = tc.parse_tool_calls(
        f'<{tag}>{{"name":"glob","arguments":{{"pattern":"*.py"}}}}</{tag}>')
    assert _names_and_args(calls) == [("glob", {"pattern": "*.py"})]


def test_format_4_markdown_fenced_json():
    """`project_fenced_json_tool_parse` — OpenAI-style instruct models fence the call."""
    content, calls = tc.parse_tool_calls(
        '```json\n{"name":"read","arguments":{"path":"/x"}}\n```')
    assert _names_and_args(calls) == [("read", {"path": "/x"})]
    assert content is None


@pytest.mark.parametrize("fence", ["json", "tool_call", "tool_code", ""])
def test_format_4_accepts_each_fence_label(fence):
    _content, calls = tc.parse_tool_calls(
        '```%s\n{"name":"read","arguments":{"path":"/x"}}\n```' % fence)
    assert _names_and_args(calls) == [("read", {"path": "/x"})]


def test_a_bare_json_call_with_no_fence_at_all_is_still_caught():
    """The last-resort fallback: some models emit the object with no wrapper of any kind."""
    _content, calls = tc.parse_tool_calls('{"name":"read","arguments":{"path":"/x"}}')
    assert _names_and_args(calls) == [("read", {"path": "/x"})]


# --------------------------------------------------------------------------- the non-greedy anchor


NESTED = ('<tool_call>{"name":"write","arguments":'
          '{"path":"/x","meta":{"deep":{"deeper":1}}}}</tool_call>')


def test_nested_arguments_parse_end_to_end():
    """Nested arguments are the normal shape of a real tool call."""
    _content, calls = tc.parse_tool_calls(NESTED)
    assert _names_and_args(calls) == [
        ("write", {"path": "/x", "meta": {"deep": {"deeper": 1}}})]


def test_branch_one_parses_nested_arguments_BY_ITSELF():
    """THIS ONE IS ISOLATED ON PURPOSE, AND THE ISOLATION IS THE WHOLE POINT.

    `\\{.*?\\}` is non-greedy: on its own it stops at the first `}` and yields invalid JSON for any
    call with nested arguments. What stretches it to the real end of the object is the trailing
    `</tool_call>` anchor.

    Measured: the end-to-end test above does NOT catch that. Loosening the anchor leaves branch 1
    matching a truncated object, `json.loads` fails, the branch quietly produces nothing — and the
    bare-JSON fallback in branch 4 re-parses the whole text and returns the right answer anyway. So
    the parser looks fine while its primary branch is dead, and stays fine only until branch 4 is
    narrowed to fenced blocks (a plausible tightening — it exists to avoid running JSON examples).

    Asserting on the regex directly is what makes the guard real."""
    matches = tc._TOOL_CALL_RE.findall(NESTED)
    assert matches, "branch 1's regex matched nothing at all"
    assert json.loads(matches[0]) == {
        "name": "write", "arguments": {"path": "/x", "meta": {"deep": {"deeper": 1}}}}, \
        "branch 1 matched a TRUNCATED object — the </tool_call> anchor is gone, and only the " \
        "branch-4 fallback is still returning the right answer"


def test_a_brace_inside_a_string_does_not_end_the_object():
    """The hand-written scanner in `_iter_json_objects` tracks strings and escapes itself. A `}` in
    a regex or a shell command is ordinary content, and truncating there yields invalid JSON —
    which is dropped silently, so the call just disappears."""
    objs = tc._iter_json_objects('{"name":"bash","arguments":{"command":"awk \'{print}\' f"}}')
    assert objs == [{"name": "bash", "arguments": {"command": "awk '{print}' f"}}]


def test_an_escaped_quote_does_not_end_the_string():
    objs = tc._iter_json_objects(r'{"a":"say \"hi\" {x}"}')
    assert objs == [{"a": 'say "hi" {x}'}]


def test_several_concatenated_objects_are_all_returned():
    assert tc._iter_json_objects('{"a":1}{"b":2}') == [{"a": 1}, {"b": 2}]


def test_a_json_array_is_flattened_to_its_items():
    assert tc._iter_json_objects('[{"a":1},{"b":2}]') == [{"a": 1}, {"b": 2}]


# --------------------------------------------------------------------------- prose must survive


def test_a_plain_answer_is_returned_untouched_with_no_calls():
    """The single most damaging failure would be eating a normal reply."""
    text = "Here is your answer. Nothing to call."
    assert tc.parse_tool_calls(text) == (text, [])


def test_prose_around_a_call_is_kept_as_content():
    content, calls = tc.parse_tool_calls(
        'Let me check.\n<tool_call>{"name":"bash","arguments":{"command":"ls"}}</tool_call>\nDone.')
    assert len(calls) == 1
    assert "Let me check." in content and "Done." in content
    assert "tool_call" not in content, "the raw call block was left in the visible reply"


def test_a_fenced_json_example_in_prose_is_not_executed():
    """Deliberate: a fenced block that is not a call SUPPRESSES the bare-JSON fallback, so an
    example the model is explaining is never run. Reading the code suggests it falls through to
    the whole text; measured, it does not — and that is the safe behaviour."""
    text = 'Here is an example:\n```json\n{"foo":"bar"}\n```\nThat is all.'
    content, calls = tc.parse_tool_calls(text)
    assert calls == []
    assert content == text


def test_empty_and_none_are_passed_straight_through():
    assert tc.parse_tool_calls("") == ("", [])
    assert tc.parse_tool_calls(None) == (None, [])


def test_a_malformed_call_block_is_not_silently_turned_into_a_call():
    """Broken JSON must fall through as prose rather than becoming a call with a null name — an
    invented tool name reaches the client as a hard error instead of a visible non-answer."""
    content, calls = tc.parse_tool_calls('<tool_call>{"name":"bash", oops}</tool_call>')
    assert calls == []
    assert content is not None


# --------------------------------------------------------------------------- argument types


def test_array_and_number_parameters_keep_their_real_types():
    """The documented failure: the native form captures every value as a raw string, so an array
    param reached the client as `Expected array, got "[{\\""` and a number as `Expected number`.
    The call is REJECTED by the client's schema — the model looks broken, not the parser."""
    _content, calls = tc.parse_tool_calls(
        "<function=ask>"
        '<parameter=questions>[{"q":1}]</parameter>'
        "<parameter=timeout>5000</parameter>"
        "<parameter=deep>true</parameter>"
        "</function>")
    args = json.loads(calls[0]["function"]["arguments"])
    assert args["questions"] == [{"q": 1}]
    assert args["timeout"] == 5000 and isinstance(args["timeout"], int)
    assert args["deep"] is True


def test_a_string_parameter_is_never_upgraded():
    """"Only upgrade when json.loads yields a non-string" — a command, a path or prose must stay a
    string. A shell command silently retyped is a call the client cannot run."""
    _content, calls = tc.parse_tool_calls(
        "<function=bash><parameter=command>ls -la</parameter></function>")
    assert json.loads(calls[0]["function"]["arguments"])["command"] == "ls -la"


@pytest.mark.parametrize("raw,expected", [
    ('"already a string"', '"already a string"'),   # a JSON string stays exactly as emitted
    ("null", None),
    ("[]", []),
    ("", ""),
])
def test_coercion_edge_values(raw, expected):
    assert tc._coerce_param_value(raw) == expected


def test_key_value_args_may_contain_spaces_and_equals_signs():
    """"A value runs until the next whitespace-delimited `key=` marker" — a regex argument is full
    of `=` and spaces, and splitting on the first one truncates the pattern into something that
    matches the wrong thing without failing."""
    args = tc._parse_tag_args("pattern=Sy|=== path=/tmp/a b")
    assert args == {"pattern": "Sy|===", "path": "/tmp/a b"}


def test_an_empty_key_value_is_dropped():
    """The model trailing off mid-call must not produce an empty required argument."""
    assert tc._parse_tag_args("path= ") == {}


def test_a_json_body_wins_over_key_value_parsing():
    assert tc._parse_tag_args('{"path":"/x","n":2}') == {"path": "/x", "n": 2}


# --------------------------------------------------------------------------- path repair


def test_a_dropped_leading_slash_is_repaired_on_path_arguments():
    """"the common small-model slip that makes a glob/read/edit silently resolve against the wrong
    cwd and find nothing" — the tool succeeds and returns nothing, so the model concludes the file
    does not exist."""
    _content, calls = tc.parse_tool_calls(
        '<tool_call>{"name":"read","arguments":{"path":"home/u/x"}}</tool_call>')
    assert json.loads(calls[0]["function"]["arguments"])["path"] == "/home/u/x"


def test_a_non_path_argument_is_never_rewritten():
    """The other half, and the dangerous one: silently editing a command or a search pattern
    changes what the tool DOES."""
    _content, calls = tc.parse_tool_calls(
        '<tool_call>{"name":"grep","arguments":'
        '{"pattern":"home/u/y","command":"home/u/z","content":"home/u/w"}}</tool_call>')
    args = json.loads(calls[0]["function"]["arguments"])
    assert args == {"pattern": "home/u/y", "command": "home/u/z", "content": "home/u/w"}


@pytest.mark.parametrize("root", ["home", "usr", "etc", "var", "opt", "tmp", "root", "mnt",
                                  "srv", "Users"])
def test_every_advertised_filesystem_root_is_repaired(root):
    assert tc._repair_path_args({"path": f"{root}/x"})["path"] == f"/{root}/x"


def test_a_relative_path_that_is_not_a_root_is_left_alone():
    """`src/main.py` is a legitimate relative path, not a dropped slash."""
    assert tc._repair_path_args({"path": "src/main.py"})["path"] == "src/main.py"


def test_path_repair_is_a_noop_on_a_non_dict():
    assert tc._repair_path_args("home/u/x") == "home/u/x"
    assert tc._repair_path_args(None) is None


# --------------------------------------------------------------------------- runaway protection


def test_identical_repeated_calls_are_deduped():
    """"296 identical 'question' calls in one response" — measured, from a real small model."""
    text = '<tool_call>{"name":"q","arguments":{"a":1}}</tool_call>' * 296
    _content, calls = tc.parse_tool_calls(text)
    assert len(calls) == 1


def test_the_total_is_capped_at_eight():
    text = "".join('<tool_call>{"name":"q","arguments":{"a":%d}}</tool_call>' % i
                   for i in range(20))
    _content, calls = tc.parse_tool_calls(text)
    assert len(calls) == 8


def test_indexes_are_renumbered_after_dedup_and_capping():
    """`index` is what the streaming client keys deltas on. A gap or a duplicate there corrupts
    the assembled call rather than dropping it."""
    text = ('<tool_call>{"name":"q","arguments":{"a":1}}</tool_call>'
            '<tool_call>{"name":"q","arguments":{"a":1}}</tool_call>'
            '<tool_call>{"name":"q","arguments":{"a":2}}</tool_call>')
    _content, calls = tc.parse_tool_calls(text)
    assert [c["index"] for c in calls] == list(range(len(calls)))


def test_a_legitimate_multi_tool_turn_is_not_collapsed():
    """Dedup must key on name AND arguments — two different reads are two real calls."""
    text = ('<tool_call>{"name":"read","arguments":{"path":"/a"}}</tool_call>'
            '<tool_call>{"name":"read","arguments":{"path":"/b"}}</tool_call>')
    _content, calls = tc.parse_tool_calls(text)
    assert len(calls) == 2


def test_every_call_gets_a_distinct_id():
    text = ('<tool_call>{"name":"read","arguments":{"path":"/a"}}</tool_call>'
            '<tool_call>{"name":"read","arguments":{"path":"/b"}}</tool_call>')
    _content, calls = tc.parse_tool_calls(text)
    assert len({c["id"] for c in calls}) == 2
    assert all(c["type"] == "function" for c in calls)


# --------------------------------------------------------------------------- name aliases


def test_a_foreign_tool_name_is_rescued_when_the_client_offers_the_equivalent():
    calls = tc._normalize_tool_names([{"function": {"name": "run_shell_command"}}],
                                     [{"function": {"name": "bash"}}])
    assert calls[0]["function"]["name"] == "bash"


def test_an_alias_is_not_applied_when_the_target_is_not_offered():
    """"only applied when the target IS in the provided tools, so it can only rescue a call the
    client would otherwise reject - never breaks a valid one"."""
    calls = tc._normalize_tool_names([{"function": {"name": "run_shell_command"}}],
                                     [{"function": {"name": "read"}}])
    assert calls[0]["function"]["name"] == "run_shell_command"


def test_a_name_the_client_already_offers_is_left_alone():
    """The rule that keeps aliasing safe: a real tool called `run` must not be renamed to `bash`
    just because an alias entry exists for that spelling."""
    calls = tc._normalize_tool_names([{"function": {"name": "run"}}],
                                     [{"function": {"name": "run"}},
                                      {"function": {"name": "bash"}}])
    assert calls[0]["function"]["name"] == "run"


def test_no_tools_offered_changes_nothing():
    for tools in (None, []):
        calls = tc._normalize_tool_names([{"function": {"name": "run_shell_command"}}], tools)
        assert calls[0]["function"]["name"] == "run_shell_command"


@pytest.mark.parametrize("alias,target", sorted(tc._TOOL_ALIASES.items()))
def test_every_alias_maps_to_a_name_that_can_be_offered(alias, target):
    """A typo'd target can never match an offered tool, so the alias silently does nothing — it
    looks wired up and rescues no call. Sweeping the real table catches the entry nobody re-read."""
    calls = tc._normalize_tool_names([{"function": {"name": alias}}],
                                     [{"function": {"name": target}}])
    assert calls[0]["function"]["name"] == target
