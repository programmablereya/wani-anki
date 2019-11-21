import os.path
import re
import json
import unicodedata
import urllib.request

from anki import find
from anki.find import Finder
from anki.hooks import addHook
from aqt import mw
from anki.sound import allSounds
from anki.utils import stripHTMLMedia, splitFields, ids2str
from aqt.qt import QAction
from aqt.utils import showInfo, showText
from urllib.error import HTTPError
from functools import lru_cache
from time import time


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
    __slots__ = ["should_searching_be_fast", "wk_api_key", "kanji_global_query", "kanji_individual_query",
                 "kanji_list_cache"]

    @classmethod
    def from_config(cls):
        return cls.from_json(mw.addonManager.getConfig(__name__))

    @classmethod
    def from_json(cls, json_obj):
        return Config(should_searching_be_fast=json_obj.get("should_searching_be_fast", False),
                      wk_api_key=json_obj.get("wk_api_key", ""),
                      kanji_global_query=json_obj.get("kanji_global_query", ""),
                      kanji_individual_query=json_obj.get("kanji_individual_query", ""),
                      kanji_list_cache=KanjiListCache.from_json(json_obj.get("kanji_list_cache", None)))

    def __init__(self, should_searching_be_fast=True, wk_api_key="", kanji_global_query="", kanji_individual_query="",
                 kanji_list_cache=None):
        self.should_searching_be_fast = should_searching_be_fast
        self.wk_api_key = wk_api_key
        self.kanji_global_query = kanji_global_query
        self.kanji_individual_query = kanji_individual_query
        self.kanji_list_cache = kanji_list_cache or KanjiListCache()

    def save(self):
        mw.addonManager.writeConfig(__name__, {
            "should_searching_be_fast": self.should_searching_be_fast,
            "wk_api_key": self.wk_api_key,
            "kanji_global_query": self.kanji_global_query,
            "kanji_individual_query": self.kanji_individual_query,
            "kanji_list_cache": self.kanji_list_cache.to_json(),
        })

    def kanji_query(self):
        return self.kanji_global_query.format(
            kanji=" or ".join(
                self.kanji_individual_query.format(kanji=kanji)
                for kanji in self.kanji_list_cache.definitions.values()))


class KanjiListCache(object):
    __slots__ = ["definitions", "last_list_etag", "last_list_update", "last_definition_etag", "last_definition_update"]

    @classmethod
    def from_json(cls, json_obj=None):
        if not json_obj:
            return
        return KanjiListCache(
            definitions=json_obj.get("definitions", None),
            last_list_etag=json_obj.get("last_list_etag", None),
            last_list_update=json_obj.get("last_list_update", None),
            last_definition_etag=json_obj.get("last_definition_etag", None),
            last_definition_update=json_obj.get("last_definition_update", None)
        )

    def __init__(self, definitions=None, last_list_etag="", last_list_update="", last_definition_etag="",
                 last_definition_update=""):
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
    list_etag, list_updated, updated_ids = get_updated_kanji_assignments(config)
    previous_ids = set(config.kanji_list_cache.definitions.keys())
    all_ids = updated_ids | previous_ids
    defs_updated, defs_etag, defs_updates = get_kanji_definitions(config, all_ids, updated=UPDATED_KANJI)
    missing_ids = updated_ids - set(updated_def_id for updated_def_id in defs_updates.keys())
    if missing_ids:
        _, _, defs_new = get_kanji_definitions(config, missing_ids, updated=NEW_KANJI)
    else:
        defs_new = ()
    if list_updated:
        config.kanji_list_cache.last_list_update = list_updated
    if list_etag:
        config.kanji_list_cache.last_list_etag = list_etag
    if defs_updated:
        config.kanji_list_cache.last_definition_update = defs_updated
    if defs_etag:
        config.kanji_list_cache.last_definition_etag = defs_etag
    if defs_updates:
        config.kanji_list_cache.definitions.update(defs_updates)
    if defs_new:
        config.kanji_list_cache.definitions.update(defs_new)
    config.save()


def get_updated_kanji_assignments(config):
    url = (
        r"https://api.wanikani.com/v2/assignments"
        r"?started=true"
        r"&subject_types=kanji")
    if config.kanji_list_cache.last_list_update:
        url += r"&updated_after=" + config.kanji_list_cache.last_list_update
    list_etag = None
    list_updated = None
    updated_ids = set()
    while url:
        list_request = urllib.request.Request(url)
        list_request.add_header("Authorization", "Bearer {wk_api_key}".format(wk_api_key=config.wk_api_key))
        list_request.add_header("Wanikani-Revision", "20170710")
        if config.kanji_list_cache.last_list_etag:
            list_request.add_header("If-None-Match", config.kanji_list_cache.last_list_etag)
        try:
            with urllib.request.urlopen(list_request) as result:
                headers = result.info()
                page = json.loads(result.read())
        except HTTPError as ex:
            if ex.code == 304:
                return None, None, set()
            raise
        list_etag = headers["ETag"]
        list_updated = page["data_updated_at"]
        updated_ids.update(str(item["data"]["subject_id"]) for item in page["data"])
        if page["pages"].get("next_url", None):
            url = page["pages"]["next_url"]
        else:
            url = None
    return list_etag, list_updated, updated_ids


NEW_KANJI = "New"
UPDATED_KANJI = "Updated"


def get_kanji_definitions(config, kanji_ids, updated=UPDATED_KANJI):
    url = (
            r"https://api.wanikani.com/v2/subjects"
            r"?types=kanji"
            r"&ids=%s" % (",".join(kanji_ids),)
    )
    if updated != NEW_KANJI and config.kanji_list_cache.last_definition_update:
        url += r"&updated_after=" + config.kanji_list_cache.last_definition_update
    etag = None
    updated = None
    updated_definitions = {}
    while url:
        list_request = urllib.request.Request(url)
        list_request.add_header("Authorization", "Bearer {wk_api_key}".format(wk_api_key=config.wk_api_key))
        list_request.add_header("Wanikani-Revision", "20170710")
        if updated != NEW_KANJI and config.kanji_list_cache.last_definition_etag:
            list_request.add_header("If-None-Match", config.kanji_list_cache.last_definition_etag)
        try:
            with urllib.request.urlopen(list_request) as result:
                headers = result.info()
                page = json.loads(result.read())
        except HTTPError as ex:
            if ex.code == 304:
                return None, None, {}
            raise
        etag = headers["ETag"]
        updated = page["data_updated_at"]
        for item in page["data"]:
            item_id = str(item["id"])
            characters = item["data"]["characters"]
            updated_definitions[item_id] = characters
        if page["pages"].get("next_url", None):
            url = page["pages"]["next_url"]
        else:
            url = None
    return updated, etag, updated_definitions


def find_unsuspendable_kanji_cards(config):
    return set(mw.col.findCards(query))


def sync_wani_kani():
    unlocked_kanji_search_time = time()
    config = Config.from_config()
    update_unlocked_kanji(config=config)
    unlocked_kanji_search_time = time() - unlocked_kanji_search_time
    card_search_time = time()
    query = config.kanji_query()
    kanji_to_unsuspend = set(mw.col.findCards(query))
    card_search_time = time() - card_search_time
    if not kanji_to_unsuspend:
        showText(
            txt=(
                "Retrieved {unlocked_kanji} unlocked kanji from WaniKani in {unlocked_kanji_search_time:f} seconds.\n\n"
                "No cards to unsuspend in {card_search_time:f} seconds when searching {speed}.\n\nUsed query:\n{query}"
            ).format(
                unlocked_kanji=len(config.kanji_list_cache.definitions),
                unlocked_kanji_search_time=unlocked_kanji_search_time,
                card_search_time=card_search_time,
                speed=("quickly" if config.should_searching_be_fast else "slowly"),
                query=query),
            copyBtn=True)
        return
    mw.col.sched.unsuspendCards(kanji_to_unsuspend)
    mw.reset()
    card_text = []
    for card_id in kanji_to_unsuspend:
        card = mw.col.getCard(card_id)
        note = card.note()
        card_text.append("cid#{id} {kanji} (Lv. {level}, {onyomi} / {kunyomi}, {meaning})".format(
            id=note.id,
            kanji=note["Kanji"],
            level=note["level"], onyomi=note["ONyomi"], kunyomi=note["KUNyomi"], meaning=note["Meaning"]))
    showText(
        txt=("Retrieved {unlocked_kanji} unlocked kanji from WaniKani in {unlocked_kanji_search_time:f} seconds.\n\n"
             "Found {unsuspend_cards} card(s) to unsuspend in {card_search_time:f} seconds "
             "when searching {speed}.\n\nUsed query:\n{query}\n\nUnsuspended cards:\n{card_list}").format(
            unlocked_kanji=len(config.kanji_list_cache.definitions),
            unlocked_kanji_search_time=unlocked_kanji_search_time,
            unsuspend_cards=len(kanji_to_unsuspend),
            card_search_time=card_search_time,
            speed=("quickly" if config.should_searching_be_fast else "slowly"),
            query=query,
            card_list="\n".join(card_text)),
        copyBtn=True)


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


@lru_cache(maxsize=None)
def normalize_field_name(name):
    return unicodedata.normalize("NFC", name.lower())


_findFieldButSlowly = find.Finder._findField


def _findFieldButFaster(field, val):
    field = normalize_field_name(field)
    val = val.replace("*", "%")
    models_with_field = [
        str(m['id']) for m in mw.col.models.all()
        if any(normalize_field_name(f['name']) == field for f in m['flds'])]
    return "n.mid in %s and field_by_model_id_and_name(n.mid, n.flds, '%s') like '%s' escape '\\'" % (
        ids2str(models_with_field), field.replace("'", "''"), val.replace("'", "''"))


class FinderButFast(find.Finder):
    def __init__(self, *args, **kwargs):
        if Config.from_config().should_searching_be_fast:
            self._findField = _findFieldButFaster
        super(FinderButFast, self).__init__(*args, **kwargs)


def make_searching_fast():
    models = mw.col.models

    def get_field_by_model_id_and_name(id, flds, name):
        m = models.get(id)
        if not m:
            return
        for f in m['flds']:
            if normalize_field_name(f['name']) == name:
                return splitFields(flds)[f['ord']]

    mw.col.db._db.create_function("field_by_model_id_and_name", 3, get_field_by_model_id_and_name)


addHook("profileLoaded", make_searching_fast)
find.Finder = FinderButFast
