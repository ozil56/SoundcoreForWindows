"""Bounded acoustic hints. The full dictionary is searched; it is never substituted into text."""
import re
from functools import lru_cache
from pypinyin import lazy_pinyin, Style


@lru_cache(maxsize=30000)
def phonetic(text):
    return ' '.join(lazy_pinyin(text.lower(),style=Style.NORMAL,errors=lambda value:list(value)))


class ContextSelector:
    def __init__(self, snapshot):
        self.general=snapshot.get('general',[])
        self.special=snapshot.get('special',[])
        self.terms=list(dict.fromkeys(self.special+self.general))
        self.index=[(term,phonetic(term)) for term in self.terms]

    def select(self, reference='', max_chars=1100, max_terms=70):
        if not self.terms: return []
        normalized=reference.lower()
        sound=' '+phonetic(reference)+' '
        ranked=[]
        for index,(term,pronunciation) in enumerate(self.index):
            score=0
            if term.lower() in normalized: score=100+min(len(term),20)
            elif len(term)>=2 and ' '+pronunciation+' ' in sound: score=80+min(len(term),20)
            if score: ranked.append((-score,index,term))
        selected=[term for _,_,term in sorted(ranked)]
        # A small explicit seed is useful before a first pass; all other terms
        # remain searchable by spelling/pronunciation after that pass.
        selected += self.special[:16]+self.general[:8]
        if len('、'.join(self.terms))<=max_chars and len(self.terms)<=max_terms:
            selected=self.terms
        output=[]; chars=0
        for term in dict.fromkeys(selected):
            if len(output)>=max_terms: break
            if chars+len(term)+1>max_chars: continue
            output.append(term); chars+=len(term)+1
        return output

    @staticmethod
    def prompt(terms):
        return '相关词汇：'+ '、'.join(terms) if terms else ''
