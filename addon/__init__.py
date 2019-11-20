import os.path
import re
import json
import urllib.request
from aqt import mw
from anki.sound import allSounds
from anki.utils import stripHTMLMedia
from aqt.qt import QAction
from collections import namedtuple


def reshow_card():
    if mw.state != "review":
        return None
    card = mw.reviewer.card
    if not card:
        return None
    ordinal = card.ord
    note = card.note(reload=True)
    matching_cards = [card for card in note.cards() if card.ord == ordinal]
    if len(matching_cards) == 1:
        mw.reviewer.cardQueue.append(matching_cards[0])
    mw.reset()


def current_note_in_review():
    if mw.state != "review":
        return None
    card = mw.reviewer.card
    if not card:
        return None
    return card.note(reload=True)


GOOD_CHARACTERS = re.compile("^[A-Za-z0-9_\- ]+\.[A-Za-z0-9_\- ]+$")

SOUND_GOOD = "Sound"
SOUND_BAD = "SoundBroken"


def toggle_sound():
    note = current_note_in_review()
    if not note or SOUND_GOOD not in note or SOUND_BAD not in note:
        return
    mediadir = mw.col.media.dir()

    def handle_filename(filename):
        fullpath = os.path.join(mediadir, filename)
        if not os.path.exists(fullpath):
            return filename, "File not found"
        # if not GOOD_CHARACTERS.match(filename):
        #     data = open(fullpath, "rb")
        #     basename, extension = os.path.splitext(filename)
        #     new_filename = "note%s-%s%s" % (note.id, base64.urlsafe_b64encode(basename), extension)
        #     new_filename = mw.col.media.writeData(opath=new_filename, data=data)
        #     return new_filename, None
        return filename, None

    good_sounds = allSounds(note[SOUND_GOOD])
    bad_sounds = allSounds(note[SOUND_BAD])
    if len(good_sounds) == 1:
        sound = good_sounds[0]
    elif len(bad_sounds) == 1:
        sound = bad_sounds[0]
    else:
        sound = None
    if not sound:
        return
    new_sound, error = handle_filename(sound)
    if error:
        note[SOUND_GOOD] = ""
        note[SOUND_BAD] = "[sound %s] (%s)" % (new_sound, error)
    else:
        note[SOUND_GOOD] = "[sound %s]" % (new_sound,)
        note[SOUND_BAD] = ""

    note.flush()
    reshow_card()


# copied from https://en.wikibooks.org/wiki/Algorithm_Implementation/Strings/Levenshtein_distance#Python
def levenshtein(s1, s2):
    if len(s1) < len(s2):
        return levenshtein(s2, s1)

    # len(s1) >= len(s2)
    if len(s2) == 0:
        return len(s1)

    previous_row = range(len(s2) + 1)
    for i, c1 in enumerate(s1):
        current_row = [i + 1]
        for j, c2 in enumerate(s2):
            insertions = previous_row[
                             j + 1] + 1  # j+1 instead of j since previous_row and current_row are one character longer
            deletions = current_row[j] + 1  # than s2
            substitutions = previous_row[j] + (c1 != c2)
            current_row.append(min(insertions, deletions, substitutions))
        previous_row = current_row

    return previous_row[-1]


PRACTICE_SENTENCE = "practice sentence"
PRACTICE_SENTENCE_THRESHOLD = 6
MEANING = "Meaning"
EXTRA_INFO = "ExtraInfo"


def swap_meaning_and_extra_info():
    note = current_note_in_review()
    if not note or MEANING not in note or EXTRA_INFO not in note:
        return
    meaning = note[MEANING]
    extra_info = note[EXTRA_INFO]

    if levenshtein(stripHTMLMedia(meaning), PRACTICE_SENTENCE) < 5:
        # PRACTICE SENTENCE!!!!
        note[MEANING] = extra_info
        note[EXTRA_INFO] = ""
    else:
        note[MEANING] = extra_info
        note[EXTRA_INFO] = meaning

    note.flush()
    reshow_card()


def wrap_list_and_add(owner, name, items):
    items = list(items)
    old = getattr(owner, name)

    def new_func(*args, **kwargs):
        return old(*args, **kwargs) + items
    setattr(owner, name, new_func)


class Config(object):
    __slots__ = ["wk_api_key", "kanji_query", "kanji_list_cache"]
    @classmethod
    def from_config(cls):
        return cls.from_json(mw.addonManager.getConfig(__name__))

    @classmethod
    def from_json(cls, json_obj):
        return Config(wk_api_key=json_obj.get("wk_api_key", ""),
                      kanji_query=json_obj.get("kanji_query", ""),
                      kanji_list_cache=KanjiListCache.from_json(json_obj.get("kanji_list_cache", None)))

    def __init__(self, wk_api_key="", kanji_query="", kanji_list_cache=None):
        self.wk_api_key = wk_api_key
        self.kanji_query = kanji_query
        self.kanji_list_cache = kanji_list_cache or KanjiListCache()

    def save(self):
        mw.addonManager.writeConfig(__name__, {
            "wk_api_key": self.wk_api_key,
            "kanji_query": self.kanji_query,
            "kanji_list_cache": self.kanji_list_cache.to_json(),
        })


class KanjiListCache(object):
    __slots__ = ["definitions", "last_list_etag", "last_list_update", "last_definition_etag", "last_definition_update"]
    @classmethod
    def from_json(cls, json_obj=None):
        if not json_obj:
            return

    def __init__(self, definitions=None, last_list_etag="", last_list_update="", last_definition_etag="", last_definition_update=""):
        self.definitions = dict(definitions or {})
        self.last_list_etag = last_list_etag
        self.last_list_update = last_list_update
        self.last_definition_etag = last_definition_etag
        self.last_definition_update = last_definition_update

    def to_json(self):
        return {
            "definitions": self.definitions,
            "last_definition_etag": self.last_definition_etag,
            "last_definition_update": self.last_definition_update,
            "last_list_etag": self.last_list_etag,
            "last_list_update": self.last_list_update}

def update_unlocked_kanji(config):
        url = (
            r"https://api.wanikani.com/v2/assignments"
            r"?started=true"
            r"&subject_types=kanji")
        if config.kanji_list_cache.last_list_update:
            url += r"&updated_after=" + config.kanji_list_cache.last_list_update
        list_etag = None
        list_updated = None
        ids = set()
        while url:
            list_request = urllib.request.Request(url)
            list_request.add_header("Authorization", "Bearer {wk_api_key}".format(wk_api_key=config.wk_api_key))
            list_request.add_header("Wanikani-Revision", "20170710")
            if config.kanji_list_cache.last_list_etag:
                list_request.add_header("If-None-Match", config.kanji_list_cache.last_list_etag)
            with urllib.request.urlopen(list_request) as result:
                code = result.getcode()
                headers = result.info()
                page = json.loads(result.read())
            if code != 200:
                raise ValueError("Got %s from list request %s: %s" % (code, url, json.dumps(page)))
            list_etag = headers["ETag"]
            list_updated = page["data_updated_at"]
            ids.add(item["id"] for item in page["data"])
            if page["pages"]["next_page"]:
                url = page["pages"]["next_page"]
            else:
                url = None
        previous_ids = set(int(cached_id) for cached_id in config.kanji_list_cache.definitions.keys())
        previous_kanji = set(config.kanji_list_cache.definitions.values())
        all_ids = ids | previous_ids
        defs_updated, defs_etag, defs_updates = get_updated_kanji_definitions(config, all_ids)
        config.kanji_list_cache.definitions.update(defs_updates)
        missing_ids = set(config.kanji_list_cache.definitions.keys()) - previous_ids
        # TODO:
        # Retrieve any items corresponding to ids which are not present in the cache without any cache specifiers
        # Update the four cache specifiers
        # Save the configuration


def get_updated_kanji_definitions(config, all_ids):
    url = (
        r"https://api.wanikani.com/v2/subjects"
        r"?types=kanji"
        r"&ids=%s" % (",".join(all_ids),)
    )
    if config.kanji_list_cache.last_definition_update:
        url += r"&updated_after=" + config.kanji_list_cache.last_definition_update
    etag = None
    updated = None
    updated_definitions = {}
    while url:
        list_request = urllib.request.Request(url)
        list_request.add_header("Authorization", "Bearer {wk_api_key}".format(wk_api_key=config.wk_api_key))
        list_request.add_header("Wanikani-Revision", "20170710")
        if config.kanji_list_cache.last_definition_etag:
            list_request.add_header("If-None-Match", config.kanji_list_cache.last_definition_etag)
        with urllib.request.urlopen(list_request) as result:
            code = result.getcode()
            headers = result.info()
            page = json.loads(result.read())
        if code != 200:
            raise ValueError("Got %s from definition request %s: %s" % (code, url, json.dumps(page)))
        etag = headers["ETag"]
        updated = page["data_updated_at"]
        for item in page["data"]:
            updated_definitions[item["id"]] = item["characters"]
        if page["pages"]["next_page"]:
            url = page["pages"]["next_page"]
        else:
            url = None
    return updated, etag, updated_definitions


def get_new_kanji_definitions(config, new_ids):
    pass


def sync_wani_kani():
    pass


new_shortcuts = (
    ["Shift+s", toggle_sound],
    ["Shift+m", swap_meaning_and_extra_info],
)


new_context_menu_items = (
    None,
    ["Toggle Sound", "Shift+S", toggle_sound],
    ["Swap Meaning and Extra Info", "Shift+M", swap_meaning_and_extra_info],
)

wrap_list_and_add(mw.reviewer, "_shortcutKeys", new_shortcuts)

wrap_list_and_add(mw.reviewer, "_contextMenu", new_context_menu_items)

action = QAction("Load WaniKani data", mw)
action.triggered.connect(sync_wani_kani)
mw.form.menuTools.addAction(action)
