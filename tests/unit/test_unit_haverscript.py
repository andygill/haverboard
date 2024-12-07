from pathlib import Path
import pytest
import sys
import json
import time
import re
import sys
from collections.abc import Iterator
import time
import threading
import subprocess

import pytest
from tenacity import stop_after_attempt

from tests.test_utils import remove_spinner

from haverscript import (
    Configuration,
    ServiceProvider,
    Middleware,
    Model,
    Response,
    LanguageModelResponse,
    connect,
    valid_json,
    LLMError,
    Service,
    LLMResultError,
    Ollama,
)
from haverscript.cache import Cache, INTERACTION

# Note that these tests break the haverscript API at points specifically
# for testing purposes.
#
# Specifically:
#   * We fake the LLM (so tests run without needing ollama).
#   * We override some fields when doing equality in the cache testing.

test_model_name = "test-model"
test_model_host = "remote.address"


def llm(host, model, messages, options, format, extra=None):
    return json.dumps(
        {
            "host": host,
            "model": model,
            "messages": messages,
            "options": options,
            "format": format,
            "extra": extra,
        },
    )


class _TestClient:
    def __init__(self, host) -> None:
        self._host = host
        self._count = 1

    def _streaming(self, reply):
        for token in re.findall(r"\S+|\s+", reply):
            time.sleep(0.01)
            yield {"message": {"content": token}, "done": False}

    def chat(self, model, stream, messages, options, format):
        assert format == "json" or format == ""
        extra = None

        assert isinstance(messages, list)
        assert len(messages) > 0
        assert isinstance(messages[-1], dict)
        assert "content" in messages[-1]
        assert isinstance(
            messages[-1]["content"], str
        ), f'expecting str, found {messages[-1]["content"]}:{type(messages[-1]["content"])}'
        if "###" in messages[-1]["content"]:
            extra = self._count
            self._count += 1

        match = re.match(r"^FAIL\((\d+)\)$", messages[-1]["content"])

        if match:
            self._count += 1
            n = int(match.group(1))
            if n != self._count:
                raise LLMError()

        reply = llm(self._host, model, messages, options, format, extra)
        if stream:
            return self._streaming(reply)
        else:
            return {
                "message": {"content": reply},
                "total_duration": 100,
                "load_duration": 101,
                "prompt_eval_count": 102,
                "prompt_eval_duration": 103,
                "eval_count": 104,
                "eval_duration": 105,
            }

    def list(self):
        return {"models": [{"name": x} for x in ["A", "B", "C"]]}


# inject the TestClient
models = sys.modules["haverscript.haverscript"].services._model_providers
models = sys.modules["haverscript.ollama"].Ollama.client = {
    None: _TestClient(None),
    test_model_host: _TestClient(test_model_host),
}


@pytest.fixture
def sample_model():
    return connect(test_model_name)


@pytest.fixture
def sample_remote_model():
    return connect(test_model_name, service=Ollama(test_model_host))


def test_model(sample_model):
    check_model(sample_model, None, None)


def test_model_and_system(sample_model):
    check_model(sample_model.system("system"), None, "system")


def test_remote_model(sample_remote_model):
    check_model(sample_remote_model, test_model_host, None)


def test_remote_model_and_system(sample_remote_model):
    check_model(sample_remote_model.system("system"), test_model_host, "system")


def check_model(model, host, system):
    assert type(model) is Model
    assert hasattr(model, "configuration")
    config = model.configuration

    render = ""
    if system:
        render = system + "\n"
    assert model.render() == render

    context = []
    if system:
        context.append({"role": "system", "content": system})
        render += "\n"
    session = model
    for i in range(2):
        message = f"Message #{i}"
        context.append({"role": "user", "content": message})
        render += "".join(["> " + line for line in message.splitlines()]) + "\n\n"
        session = session.chat(message)
        assert type(session) is Response
        assert isinstance(session.reply, str)
        assert session.reply == llm(host, test_model_name, context, {}, "")
        assert session.metrics.total_duration == 100
        assert session.metrics.load_duration == 101
        assert session.metrics.prompt_eval_count == 102
        assert session.metrics.prompt_eval_duration == 103
        assert session.metrics.eval_count == 104
        assert session.metrics.eval_duration == 105

        context.append({"role": "assistant", "content": session.reply})
        render += session.reply
        render += "\n"
        assert session.render() == render
        render += "\n"


class UserService(ServiceProvider):
    def chat(self, prompt: str, **args):
        return LanguageModelResponse(
            [
                f"I reject your {len(prompt.split())} word prompt, and replace it with my own."
            ]
        )

    def list(self):
        return ["A", "B", "C"]


@pytest.fixture
def sample_user_model():
    return connect("some-model", service=UserService())


def test_user_model(sample_user_model):
    model = sample_user_model
    assert isinstance(model, Model)
    assert hasattr(model, "configuration")
    config = model.configuration
    context = []
    session = model
    message = "Three word prompt"
    context.append({"role": "user", "content": message})
    session = session.chat(message)
    assert type(session) is Response
    assert isinstance(session.reply, str)
    assert session.reply == "I reject your 3 word prompt, and replace it with my own."


def test_list_models():
    service = connect()
    assert isinstance(service, Service)
    assert service.list() == ["A", "B", "C"]


reply_to_hello = """
> Hello

{"host": null, "model": "test-model", "messages": [{"role": "user", "content":
"Hello"}], "options": {}, "format": "", "extra": null}
"""

reply_to_hello_48 = """
> Hello

{"host": null, "model": "test-model",
"messages": [{"role": "user", "content":
"Hello"}], "options": {}, "format": "", "extra":
null}
"""
reply_to_hello_8 = """
> Hello

{"host":
null,
"model":
"test-model",
"messages":
[{"role":
"user",
"content":
"Hello"}],
"options":
{},
"format":
"",
"extra":
null}
"""

reply_to_hello_no_prompt = """
{"host": null, "model": "test-model", "messages": [{"role": "user", "content":
"Hello"}], "options": {}, "format": "", "extra": null}
""".lstrip()


def test_echo(sample_model, capfd):
    capfd.readouterr()

    resp = sample_model.chat("Hello")
    assert capfd.readouterr().out == ""

    for line in reply_to_hello.splitlines():
        assert len(line) <= 78

    resp = sample_model.echo().chat("Hello")
    assert remove_spinner(capfd.readouterr().out) == reply_to_hello

    resp = sample_model.echo(spinner=False).chat("Hello")
    assert capfd.readouterr().out == reply_to_hello

    for line in reply_to_hello_48.splitlines():
        assert len(line) <= 48

    resp = sample_model.echo(width=48).chat("Hello")
    assert remove_spinner(capfd.readouterr().out) == reply_to_hello_48

    for line in reply_to_hello_8.splitlines():
        assert len(line) <= 8 or " " not in line

    resp = sample_model.echo(width=8).chat("Hello")
    assert remove_spinner(capfd.readouterr().out) == reply_to_hello_8

    resp = sample_model.echo(prompt=False).chat("Hello")
    assert remove_spinner(capfd.readouterr().out) == reply_to_hello_no_prompt


def test_stats(sample_model, capfd):
    capfd.readouterr()

    sample_model.stats().chat("Hello")
    txt = remove_spinner(capfd.readouterr().out)
    pattern = r"prompt\s*:\s*\d+b,\s*reply\s*:\s*\d+t,\s*first\s*token\s*:\s*\d+(\.\d+)?s,\s*tokens\/s\s*:\s*\d+"
    assert re.search(pattern, txt), f"found: {txt}"

    capfd.readouterr()
    try:
        sample_model.stats().chat("FAIL(1)")
    except Exception:
        ...
    txt = remove_spinner(capfd.readouterr().out)
    assert txt == "- prompt : 7b, LLMError Exception raised\n"


def test_outdent(sample_model):
    messages = [
        "Hello",
        """
    Hello

    World
    """,
    ]
    model = sample_model
    for ix, model in enumerate(
        [
            sample_model,
            sample_model.outdent(True),
            sample_model.outdent(False),
        ]
    ):
        for message in messages:
            reply = model.chat(message)
            actual_message = message
            if ix != 2:
                actual_message = "\n".join(
                    [line.lstrip() for line in actual_message.splitlines()]
                ).strip()
            context = [{"role": "user", "content": actual_message}]
            assert reply.reply == llm(None, test_model_name, context, {}, "")


def test_options(sample_model):
    reply = sample_model.options(seed=12345).chat("")
    context = [{"role": "user", "content": ""}]
    assert reply.reply == llm(None, test_model_name, context, {"seed": 12345}, "")


def test_system(sample_model):
    reply = sample_model.system("woof!").chat("")
    context = [{"role": "system", "content": "woof!"}, {"role": "user", "content": ""}]
    assert reply.reply == llm(None, test_model_name, context, {}, "")


def test_json(sample_model):
    for ix, model in enumerate(
        [
            sample_model,
            sample_model.json(True),
            sample_model.json(False),
        ]
    ):
        reply = model.chat("")
        context = [{"role": "user", "content": ""}]
        assert reply.reply == llm(
            None, test_model_name, context, {}, "json" if ix == 1 else ""
        )
        if ix == 1:
            assert reply.value is not None
            assert reply.value == json.loads(
                llm(None, test_model_name, context, {}, "json" if ix == 1 else "")
            )


def test_cache(sample_model, tmp_path):
    temp_file = tmp_path / "cache.db"
    mode = "a+"
    model = sample_model.cache(temp_file, mode)

    hello = "### Hello"
    assert len(model.children(hello)) == 0
    assert len(model.children()) == 0

    reply1 = model.chat(hello)
    assert len(model.children(hello)) == 1
    assert len(model.children()) == 1
    assert model.children(hello)[0] == reply1.copy(fresh=False, metrics=None)

    reply2 = model.chat(hello)
    assert len(model.children(hello)) == 2
    assert len(model.children()) == 2
    assert model.children(hello)[1] == reply2.copy(fresh=False, metrics=None)

    world = "### World"
    reply3 = reply2.chat(world)
    assert len(reply2.children()) == 1
    assert len(reply2.children(world)) == 1
    assert reply2.children(world)[0] == reply3.copy(fresh=False, metrics=None)

    # reset the cursor, to simulate a new execute
    sys.modules["haverscript.cache"].Cache.connections = {}
    model = sample_model.cache(temp_file, mode)

    assert len(model.children()) == 2
    reply1b = model.chat(hello)
    assert reply1.copy(metrics=None) == reply1b.copy(metrics=None)

    reply2b = model.chat(hello)

    assert reply2.copy(metrics=None) == reply2b.copy(metrics=None)
    # Check there was a difference to observe
    assert reply1b.copy(metrics=None) != reply2b.copy(metrics=None)

    # now test the read mode
    sys.modules["haverscript.cache"].Cache.connections = {}
    mode = "r"
    model = sample_model.cache(temp_file, mode)

    assert len(model.children()) == 2
    reply1b = model.chat(hello)

    scrub = dict(configuration=None, settings=None, parent=None)

    assert reply1.copy(metrics=None, **scrub) == reply1b.copy(metrics=None, **scrub)

    reply2b = model.chat(hello)

    assert reply2.copy(metrics=None, **scrub) == reply2b.copy(metrics=None, **scrub)

    # Check they are the same in read mode
    assert reply1b.copy(metrics=None) == reply2b.copy(metrics=None)

    sys.modules["haverscript.cache"].Cache.connections = {}
    mode = "a"
    model = sample_model.cache(temp_file, mode)

    hello = "### Hello"
    assert len(model.children(hello)) == 0
    assert len(model.children()) == 0

    reply1 = model.chat(hello)
    # Nothing to read, this is append mode
    assert len(model.children(hello)) == 0
    assert len(model.children()) == 0


def test_check(sample_model):
    # simple check
    assert repr(
        sample_model.validate(lambda reply: "Squirrel" in reply).chat("Squirrel")
    ).startswith("Response")

    # failing check
    with pytest.raises(LLMResultError):
        sample_model.validate(lambda reply: "Squirrel" not in reply).chat(
            "Squirrel"
        ).retry(stop=stop_after_attempt(5))

    # chaining checks
    sample_model.validate(
        lambda reply: "Squirrel" in reply
    ).validate(  # add on check for Haggis
        lambda reply: "Haggis" in reply
    ).validate(
        valid_json
    ).chat(  # check output is valid JSON (the test stub used JSON for output)
        "Squirrel Haggis"
    )


def valid_json(txt):
    try:
        json.loads(str(txt))
        return True
    except json.JSONDecodeError as e:
        raise False


def test_image(sample_model):
    image_src = f"{Path(__file__).parent}/../examples/images/edinburgh.png"
    prompt = "Describe this image"
    resp = sample_model.image(image_src).chat(prompt)
    context = [{"role": "user", "content": prompt, "images": [image_src]}]
    reply = resp.reply
    assert reply == llm(None, test_model_name, context, {}, "")
    prompt2 = "Follow on question"
    resp = resp.chat(prompt2)
    context = [
        {"role": "user", "content": prompt, "images": [image_src]},
        {"role": "assistant", "content": reply},
        {"role": "user", "content": prompt2},
    ]
    assert resp.reply == llm(None, test_model_name, context, {}, "")


def test_reject(sample_model):
    with pytest.raises(LLMResultError):
        sample_model.chat("Hello").reject()


def test_retry(sample_model):
    with pytest.raises(LLMError):
        sample_model.chat("FAIL(0)")

    extra = sample_model.json().chat("###").value["extra"]
    sample_model.retry(stop=stop_after_attempt(5)).chat(f"FAIL({extra+4})")


def test_validate(sample_model):
    with pytest.raises(LLMError):
        sample_model.validate(lambda txt: "$$$" in txt).chat("...")

    sample_model.validate(lambda txt: "$$$" in txt).chat("$$$")

    extra = sample_model.json().chat("###").value["extra"]

    with pytest.raises(LLMError):
        sample_model.validate(lambda txt: f'"extra": {extra + 10}' in txt).retry(
            stop=stop_after_attempt(5)
        ).chat("###")

    extra = sample_model.json().chat("###").value["extra"]

    sample_model.validate(lambda txt: f'"extra": {extra + 3}' in txt).retry(
        stop=stop_after_attempt(5)
    ).chat("###")

    extra = sample_model.json().chat("###").value["extra"]

    with pytest.raises(LLMError):
        # wrong order; retry is bellow validate.
        sample_model.retry(stop=stop_after_attempt(5)).validate(
            lambda txt: f'"extra": {extra + 3}' in txt
        ).chat("###")


#


md0 = "System"

md1 = """
> Hello
World
"""

md2 = """
System
> Hello
World
> Sprite
"""


def test_load(sample_model):
    session = sample_model.load(md0)
    assert isinstance(session, Model) and not isinstance(session, Response)

    session = sample_model.load(md1)
    assert isinstance(session, Response)
    assert session.configuration.system is None
    assert session.prompt == "Hello"
    assert session.reply == "World"

    session = sample_model.load(md2)
    assert isinstance(session, Response)
    assert session.configuration.system == "System"
    assert session.prompt == "Sprite"
    assert session.reply == ""

    session = sample_model.load(md2, complete=True)
    assert isinstance(session, Response)
    assert session.configuration.system == "System"
    assert session.prompt == "Sprite"
    context = [
        {"role": "system", "content": "System"},
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "World"},
        {"role": "user", "content": "Sprite"},
    ]
    assert session.reply == llm(None, test_model_name, context, {}, "")


class UpperCase(Middleware):

    def upper_tokens(self, responses):
        for token in responses:
            if isinstance(token, str):
                yield token.upper()

    def chat(self, prompt: str, **ksargs):
        responses = self.next.chat(prompt + " World", **ksargs)
        return LanguageModelResponse(str(responses).upper())


def test_middleware(sample_model: Model):
    reply0 = sample_model.chat("Hello" + " World")

    session = sample_model.middleware(UpperCase)
    reply1 = session.chat("Hello")
    reply2 = session.retry(stop=stop_after_attempt(5)).chat("Hello")

    assert reply0.reply.upper() == reply1.reply
    assert reply0.reply.upper() == reply2.reply


def test_LanguageModelResponse():
    """Test that LanguageModelResponse responses in a thread-safe manner"""

    def gen(xs):
        for x in xs:
            time.sleep(0.01)
            yield f"{x} "

    input = list(range(10))

    lmr = LanguageModelResponse(gen(input))

    def consume():
        result = []
        for x in lmr.tokens():
            result.append(int(x))
        assert result == input

    def metrics():
        assert lmr.metrics() is None

    ts = []
    for n in range(10):
        if n == 4:
            fun = metrics
        else:
            fun = consume
        ts.append(threading.Thread(target=fun))

    for t in ts:
        t.start()

    for t in ts:
        t.join()


def test_cache_class(tmp_path):
    temp_file = tmp_path / "cache.db"
    cache = Cache(temp_file, "a+")
    system = "..."
    context = (("Hello", [], "World"), ("Hello2", [], "World2"))
    prompt = "Hello!"
    images = ["foo.png"]
    reply = "Wombat"
    options = {"model": "modal"}
    cache.insert_interaction(system, context, prompt, images, reply, options)
    cache.insert_interaction(system, context, prompt, images, reply, options)
    cache.insert_interaction(
        system, context, prompt + "..2", images, reply + "..2", options
    )

    # pretend that the database is read fresh
    cache.conn.execute("DELETE FROM blacklist;")

    assert cache.lookup_interactions(
        system, context, prompt, images, options, limit=None, blacklist=False
    ) == {INTERACTION(1): (prompt, images, reply)}

    cache.blacklist(INTERACTION(1))
    # Should not appear in our lookup (because of the blacklist)

    cache.insert_interaction(
        system, context, prompt, images, reply + " (Again)", options
    )

    assert (
        cache.lookup_interactions(
            system, context, prompt, images, options, limit=None, blacklist=True
        )
        == {}
    )
    assert cache.lookup_interactions(
        system, context, None, images, options, limit=None, blacklist=False
    ) == {
        INTERACTION(1): (prompt, images, reply),
        INTERACTION(2): (prompt + "..2", images, reply + "..2"),
        INTERACTION(3): (prompt, images, reply + " (Again)"),
    }

    assert cache.lookup_interactions(
        system, context, None, images, options, limit=1, blacklist=False
    ) == {
        INTERACTION(1): (prompt, images, reply),
    }
    result = subprocess.run(
        f'echo ".dump" | sqlite3 {temp_file}',
        shell=True,
        text=True,
        capture_output=True,
    )
    result = "\n".join([line.rstrip() for line in result.stdout.splitlines()])

    assert result == sql_dump


sql_dump = """
PRAGMA foreign_keys=OFF;
BEGIN TRANSACTION;
CREATE TABLE string_pool (
    id INTEGER PRIMARY KEY,
    string TEXT NOT NULL UNIQUE
);
INSERT INTO string_pool VALUES(1,'Hello');
INSERT INTO string_pool VALUES(2,'[]');
INSERT INTO string_pool VALUES(3,'World');
INSERT INTO string_pool VALUES(4,'Hello2');
INSERT INTO string_pool VALUES(5,'World2');
INSERT INTO string_pool VALUES(6,'Hello!');
INSERT INTO string_pool VALUES(7,'["foo.png"]');
INSERT INTO string_pool VALUES(8,'Wombat');
INSERT INTO string_pool VALUES(9,'...');
INSERT INTO string_pool VALUES(10,'{"model": "modal"}');
INSERT INTO string_pool VALUES(11,'Hello!..2');
INSERT INTO string_pool VALUES(12,'Wombat..2');
INSERT INTO string_pool VALUES(13,'Wombat (Again)');
CREATE TABLE context (
    id INTEGER PRIMARY KEY,
    prompt INTEGER  NOT NULL,       -- what was said to the LLM
    images INTEGER NOT NULL,        -- string of list of images
    reply INTEGER NOT NULL,         -- reply from the LLM
    context INTEGER,
    FOREIGN KEY (prompt)        REFERENCES string_pool(id),
    FOREIGN KEY (images)        REFERENCES string_pool(id),
    FOREIGN KEY (reply)         REFERENCES string_pool(id),
    FOREIGN KEY (context)       REFERENCES interactions(id)
);
INSERT INTO context VALUES(1,1,2,3,NULL);
INSERT INTO context VALUES(2,4,2,5,1);
INSERT INTO context VALUES(3,6,7,8,2);
INSERT INTO context VALUES(4,11,7,12,2);
INSERT INTO context VALUES(5,6,7,13,2);
CREATE TABLE interactions (
    id INTEGER PRIMARY KEY,
    system INTEGER,
    context INTEGER,
    parameters INTEGER NOT NULL,
    FOREIGN KEY (system)        REFERENCES string_pool(id),
    FOREIGN KEY (context)       REFERENCES context(id),
    FOREIGN KEY (parameters)    REFERENCES string_pool(id)
);
INSERT INTO interactions VALUES(1,9,3,10);
INSERT INTO interactions VALUES(2,9,4,10);
INSERT INTO interactions VALUES(3,9,5,10);
CREATE INDEX string_index ON string_pool(string);
CREATE INDEX context_prompt_index ON context(prompt);
CREATE INDEX context_images_index ON context(images);
CREATE INDEX context_reply_index ON context(reply);
CREATE INDEX context_context_index ON context(context);
CREATE INDEX interactions_system_index ON interactions(system);
CREATE INDEX interactions_context_index ON interactions(context);
CREATE INDEX interactions_parameters_index ON interactions(parameters);
COMMIT;
""".strip()
