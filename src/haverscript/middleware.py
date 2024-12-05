import re
import threading
import queue
import time

from dataclasses import dataclass
from typing import AnyStr, Callable, Optional, Self, Tuple, NoReturn

from tenacity import Retrying, RetryError
from yaspin import yaspin

from .exceptions import LLMResultError
from .languagemodel import LanguageModel, LanguageModelResponse


@dataclass(frozen=True)
class Middleware(LanguageModel):
    """Middleware is a LanguageModel that has next down-the-pipeline LanguageModel."""

    next: LanguageModel


@dataclass(frozen=True)
class ModelMiddleware(Middleware):
    model: str

    def chat(self, prompt: str, **kwargs):
        return self.next.chat(prompt, model=self.model, **kwargs)


@dataclass(frozen=True)
class RetryMiddleware(Middleware):
    """Retry the lower chat if it fails, using options as argument(s) to tenacity retry.

    There is a cavet here. If the lower chat returns an in-progress and working streaming,
    then this will be accepted by this retry. We wait for the first token, though.
    """

    options: dict

    def chat(self, prompt: str, **kwargs):
        try:
            for attempt in Retrying(**self.options):
                with attempt:
                    # We turn off streaming, because we want complete results.
                    return self.next.chat(prompt, streaming=False, **kwargs)
        except RetryError as e:
            raise LLMResultError()


@dataclass(frozen=True)
class ValidationMiddleware(Middleware):
    """Validate if a predicate is true for the response."""

    predicate: Callable[[str], bool]

    def chat(self, prompt: str, **kwargs):
        response = self.next.chat(prompt, **kwargs)
        # the str forces the full evaluation here
        if not self.predicate(str(response)):
            raise LLMResultError()
        return response


@dataclass(frozen=True)
class EchoMiddleware(Middleware):
    width: int = 78
    prompt: bool = True

    def chat(self, prompt: str, **kwargs):
        if self.prompt:
            print()
            print("\n".join([f"> {line}" for line in prompt.splitlines()]))
            print()

        event = threading.Event()

        def wait_for_event():
            with yaspin() as spinner:
                event.wait()  # Wait until the event is set

        spinner_thread = threading.Thread(target=wait_for_event)
        spinner_thread.start()

        try:
            # We turn on streaming to make the echo responsive
            kwargs.pop("stream", None)
            response = self.next.chat(prompt, stream=True, **kwargs)
            assert isinstance(response, LanguageModelResponse)
        finally:
            # stop the spinner
            event.set()
            # wait for the spinner thread to stop (and stop printing)
            spinner_thread.join()

        for token in self._wrap(response.tokens()):
            print(token, end="", flush=True)
        print()  # finish with a newline

        return response

    def _wrap(self, stream):

        line_width = 0  # the size of the commited line so far
        spaces = 0
        newline = "\n"
        prequel = ""

        for s in stream:

            for t in re.split(r"(\n|\S+)", s):

                if t == "":
                    continue

                if t.isspace():
                    if line_width + spaces + len(prequel) > self.width:
                        yield newline  # injected newline
                        line_width = 0
                        spaces = 0

                    if spaces > 0 and line_width > 0:
                        yield " " * spaces
                        line_width += spaces

                    spaces = 0
                    line_width += spaces + len(prequel)
                    yield prequel
                    prequel = ""

                    if t == "\n":
                        line_width = 0
                        yield newline  # actual newline
                    else:
                        spaces += len(t)
                else:
                    prequel += t

        if prequel != "":
            if line_width + spaces + len(prequel) > self.width:
                yield newline
                line_width = 0
                spaces = 0

            if spaces > 0 and line_width > 0:
                yield " " * spaces

            yield prequel

        return

    def list(self):
        return []
