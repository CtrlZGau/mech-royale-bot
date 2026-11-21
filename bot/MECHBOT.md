# Vendored: mechbot

`mb_bot.py` and `mb_world.py` are `bot.py` and `world.py` from https://github.com/Specz04/mechbot
at commit `120f1ff` ("Practice: retreat and heal in cover, break off losing duels earlier, grab
cubes mid-fight"), MIT license (`LICENSE-mechbot`). Only change: `from world import` ->
`from mb_world import` (our `bot/world.py` has the same module name). Strategy write-up:
the `MECHBOT_POLICIES.md` document.
