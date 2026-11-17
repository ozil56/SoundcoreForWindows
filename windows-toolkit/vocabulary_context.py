"""Retrieve acoustic hints from the full snapshot; never substitute output text."""
import hashlib
import unicodedata
from functools import lru_cache
from pypinyin import lazy_pinyin, Style

STRATEGY = 'full-vocabulary-spelling-pinyin-near-v2'
MAX_TERMS = 70
MAX_CHARS = 1100


@lru_cache(maxsize=30000)
def phonetic(text):
    return ' '.join(lazy_pinyin(text.lower(),style=Style.NORMAL,errors=lambda value:list(value)))


def normalized(text):
    return ''.join(ch for ch in text.lower() if ch.isalnum())


class _NearSounds:
    """Index query windows once, instead of rescanning speech for every library term."""
    def __init__(self, words, lengths):
        self.short = {}; self.same = {}; self.long = {}
        for length in lengths:
            self.short[length] = {tuple(words[i:i+length-1]) for i in range(len(words)-length+2)}
            same = set(); longer = set()
            for i in range(len(words)-length+1):
                window = tuple(words[i:i+length])
                for at in range(length):
                    same.add((at, window[:at]+window[at+1:]))
            for i in range(len(words)-length):
                window = tuple(words[i:i+length+1])
                for at in range(length+1):
                    longer.add(window[:at]+window[at+1:])
            self.same[length] = same; self.long[length] = longer

    def contains(self, syllables):
        length = len(syllables)
        if syllables in self.long[length]:
            return True
        for at in range(length):
            removed = syllables[:at]+syllables[at+1:]
            if removed in self.short[length] or (at, removed) in self.same[length]:
                return True
        return False


class ContextSelector:
    def __init__(self, snapshot):
        self.general = snapshot.get('general', [])
        self.special = snapshot.get('special', [])
        self.terms = []; seen = set()
        for term in self.special+self.general:
            key = unicodedata.normalize('NFKC', term).strip().casefold()
            if key and key not in seen:
                self.terms.append(term); seen.add(key)
        self.index = [(term, phonetic(term)) for term in self.terms]
        self.entries = [(term, normalized(term), sound, tuple(sound.split()),
                         hashlib.sha256(term.encode()).hexdigest()) for term, sound in self.index]

    def select(self, reference='', max_chars=MAX_CHARS, max_terms=MAX_TERMS):
        # No first-N seed, even for a small dictionary. Empty/unrelated speech gets no hints.
        plain = normalized(reference)
        if not plain or not self.terms:
            return []
        max_terms = max(0, min(MAX_TERMS, int(max_terms)))
        max_chars = max(0, min(MAX_CHARS, int(max_chars)))
        if not max_terms or not max_chars:
            return []
        words = phonetic(reference).split()
        sound = ' '+' '.join(words)+' '
        ranked = []; unmatched = []
        for term, key, pronunciation, syllables, tie in self.entries:
            if not key:
                continue
            if key in plain:
                ranked.append((-100, -len(term), tie, term))
            elif len(term) >= 2 and ' '+pronunciation+' ' in sound:
                ranked.append((-95, -len(term), tie, term))
            elif 4 <= len(syllables) <= 10 and all('\u4e00' <= ch <= '\u9fff' for ch in term):
                unmatched.append((term, syllables, tie))
        if unmatched:
            near = _NearSounds(words, {len(syllables) for _, syllables, _ in unmatched})
            for term, syllables, tie in unmatched:
                if near.contains(syllables):
                    ranked.append((-70, -len(term), tie, term))
        output = []; chars = 0
        for _, _, _, term in sorted(ranked):
            if len(output) >= max_terms:
                break
            size = len(term)+1
            if chars+size <= max_chars:
                output.append(term); chars += size
        return output

    @staticmethod
    def prompt(terms):
        return '相关词汇：'+ '、'.join(terms) if terms else ''
