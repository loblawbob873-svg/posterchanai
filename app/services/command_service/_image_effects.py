"""Image effects whose chat command is only "hand the attached pictures to `<name>_attachments`".

Eighty-eight `_<name>_command` methods were one copied body each: refuse with a hint when no picture is
attached, otherwise run `effects_service.<name>_attachments` off the event loop and return its files or its
message. They are generated here from the names, under the same method names, and `execute_command` routes
them with one membership test. An effect that reads its argument (`glow`, `meme`, `mentioned`, …) keeps a
hand-written command.
"""
from typing import Optional

IMAGE_EFFECT_COMMANDS = frozenset({
    "adamsfamily", "akbar", "barked", "beavis", "bike", "blacked", "blood", "blue", "bullethole",
    "charliesangles", "cheers", "chimp", "clay", "collage", "consider", "cum", "curb", "darkness",
    "depressing", "diarrhea", "differentstroke", "dildo", "dontwanttowait", "fahh", "fbi", "feliz",
    "feltedtables", "fire", "freebird", "futurama", "gay", "gigity", "gong", "goon", "gura", "hag",
    "happydays", "harlem", "hava", "heat", "helpme", "hood", "horse", "hugebitch", "indian", "jerry", "jobs",
    "kanye", "knightrider", "kosher", "liberal", "makima", "mixalot", "moving", "munsters", "nakedman",
    "nami", "nonematters", "onepiece", "overtaken", "poo", "prayer", "rebecca", "redeem", "ree", "retard",
    "reze", "robocop", "seinfeld", "seth", "shrug", "sleepwell", "smell", "sopranos", "strangerthings",
    "terminator", "theraped", "thug", "titan", "uwu", "vibe", "wasteland", "whoabuddy", "woodchipper",
    "would", "xmen", "yakety", "yamete",
})

# What to say when no picture is attached, where it is more than "Attach an image, then send `<name>`."
_ASK = {
    'blood': 'Attach an image, then send `blood` to decorate it.',
    'collage': 'Attach two or more images, then send `collage` to combine them.',
    'cum': 'Attach an image, then send `cum` to decorate it.',
    'dildo': 'Attach an image, then send `dildo` to decorate it.',
    'poo': 'Attach an image, then send `poo` to decorate it.',
}


def _command(name: str):
    ask = _ASK.get(name, f"Attach an image, then send `{name}`.")

    async def run(self, attachments: Optional[list]) -> dict:
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": ask}

        import asyncio
        from app.services import effects_service

        # Read at call time, as the hand-written `from … import <name>_attachments` was.
        outputs, summary = await asyncio.to_thread(getattr(effects_service, f"{name}_attachments"), attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    run.__name__ = run.__qualname__ = f"_{name}_command"
    return run


class _ImageEffectsMixin:
    IMAGE_EFFECT_COMMANDS = IMAGE_EFFECT_COMMANDS


for _name in IMAGE_EFFECT_COMMANDS:
    setattr(_ImageEffectsMixin, f"_{_name}_command", _command(_name))
del _name
