# Humble Steam Key Redeemer
Python utility script to extract Humble keys, and redeem them on Steam automagically by detecting when a game is already owned on Steam.

This is primarily designed to be a set-it-and-forget-it tool that maximizes successful entry of keys into Steam, assuring that no Steam game goes unredeemed.

This script will login to both Humble and Steam, automating the whole process. It's not perfect as I made this mostly for my own personal case and couldn't test all possibilities so YMMV. Feel free to send submit an issue if you do bump into issues.

Any revealing and redeeming the script does will output to spreadsheet files based on their actions for you to easily review what actions it took and whether it redeemed, skipped, or failed on specific keys.

## Modes
### Auto-Redeem Mode (Steam)
Find Steam games from Humble that are unowned by your Steam user, and ONLY of those that are unowned, redeem on Steam revealed keys (This EXCLUDES non-Steam keys and unclaimed Humble Choice games)

If you choose to reveal keys in this mode, it will only reveal keys that it goes to redeem (ignoring those that are detected as already owned)
### Export Mode
Find all games from Humble, optionally revealing all unrevealed keys, and output them to a CSV (comes with an optional Steam ownership column). 

This is great if you want a manual review of what games are in your keys list that you may have missed.
### Humble Chooser Mode
For those subscribed to Humble Choice, this mode will find any Humble Monthly/Choice that has unclaimed choices, and will let you select, reveal, and optionally autoredeem on Steam the keys you select

#
### Verify Mode

Report-only cross-check of the whole Humble Steam key list against your
current Steam ownership: every revealed key is checked by its app id
(title matching only where Humble supplies no id), and the result is
written to `steam_keys_status.csv`. Keys your Steam account does *not*
own despite being revealed -- a past run that recorded a redeem Steam
never applied, or a key redeemed elsewhere/gifted -- surface as suspects
for review, as do keys recorded in `errored.csv`/`already_owned.csv`
that you in fact do not own. Nothing is redeemed in this mode; run it
with no login prompt at all when the Web API key + SteamID64 pair is set.
from the menu or with `python humblesteamkeysredeemer.py --verify`.

### Attempt-gate (avoiding rate-limit burns)

Failed Steam activations ("already owned", dead keys) count against a
~10-per-hour limit, so before any key is attempted the tool exhausts what
it can know without activating:

- keys whose Humble `steam_app_id` is already owned are skipped;
- for keys Humble ships **without** an app id, the app's own title is looked
  up in the store catalogue fetched that run -- an exact store-name match
  that you already own is skipped without spending quota (this catches
  store-delisted entries that title matching against *owned names* cannot
  see, including bilingual store names like `STRIDER / ストライダー飛竜`);
- multi-product packs that no store name resolves ("Torchlight 1-3 Bundle")
  and near-miss titles are still attempted: Steam's own activation answer is
  the only oracle for those, and each answer is recorded so no key is ever
  attempted twice.
- before the attempts begin, the full intent list is printed with a
  `[y/N]` confirm; pass `--yes` to skip that prompt.

### Notes

To remove an already added account, delete the associated `.(humble|steam)cookies` file.

### Dependencies

Requires Python 3.8 or above (selenium 4.10 needs it; known-good on 3.13 and 3.15).
Optional but strongly recommended: a Steam Web API key in `steam_api_key.txt`
(https://steamcommunity.com/dev/apikey) for much faster ownership matching.
Add your 17-digit SteamID64 (or your profile URL) on a **second line** and
ownership is read directly through the Web API: no Steam sign-in session is
needed at all, and `--verify` runs without any login. Delisted games are
included in that list, so the name-resolution ladder rarely has work left.

- `steam`: [ValvePython/steam](https://github.com/ValvePython/steam)  
- `fuzzywuzzy`: [seatgeek/fuzzywuzzy](https://github.com/seatgeek/fuzzywuzzy)  
- `requests`: [requests](https://requests.readthedocs.io/en/master/)
- `selenium`: [selenium](https://www.selenium.dev/)
- `pwinput`: [pwinput](https://github.com/asweigart/pwinput)
- `python-Levenshtein`: [rapidfuzz/python-Levenshtein](https://github.com/rapidfuzz/python-Levenshtein)  

Install the required dependencies with
```
pip install -r requirements.txt
```

`python-Levenshtein` backs `fuzzywuzzy` with a C implementation. It used to be
optional, but matching every Humble key against a large Steam library is slow
enough on the pure-python fallback to be worth requiring. It installs from a
prebuilt wheel, so no compiler is needed.
