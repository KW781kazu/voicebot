# fixed_replies.py
# 固定フレーズ応答ロジック（営業時間・住所・定休日など）
import re

# ここをあなたの店舗情報に。住所はご指定のものを反映しています。
INFO = {
    "name": "フロントガラス修理ボット",
    "hours": "平日9:00〜18:00",
    "closed": "日曜・祝日",
    "address": "埼玉県上尾市菅谷3-4-1",
    "phone": "050-5454-5454",
}

def _norm(s: str) -> str:
    """簡易正規化：大小・全角空白・記号などのゆらぎを軽減"""
    s = (s or "").lower()
    s = s.replace("　", " ").replace("\u3000", " ")
    s = re.sub(r"[^\wぁ-んァ-ン一-龥 ]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def reply_fixed(user_utterance: str) -> str | None:
    """
    ユーザー発話に対して、該当すれば決め打ちの返答を返す。
    合致なしなら None を返して、呼び出し側のデフォルト応答にフォールバック。
    """
    t = _norm(user_utterance)

    # 営業時間
    if re.search(r"(営業時間|何時|いつ.*(開|やっ)|open|時間|何時まで|何時から)", t):
        return f"営業時間は{INFO['hours']}、定休日は{INFO['closed']}です。"

    # 住所/場所/アクセス
    if re.search(r"(どこ|住所|場所|所在地|アクセス|行き方|地図|最寄り)", t):
        base = f"所在地は{INFO['address']}です。"
        if INFO["phone"] and "電話" in t:
            return base + f" お電話は{INFO['phone']}へお願いします。"
        return base

    # 定休日
    if re.search(r"(定休日|休み|休業日|休店日)", t):
        return f"定休日は{INFO['closed']}です。"

    # 料金の目安（ダミー例）
    if re.search(r"(料金|値段|費用|いくら|価格)", t):
        return "ガラスの種類やサイズで費用が変わります。概算は一万円台からです。詳しくは内容をお聞かせください。"

    # あいさつ
    if re.search(r"(はじめまして|こんにちは|もしもし|おはよう|こんばんは)", t):
        return f"お電話ありがとうございます。{INFO['name']}です。ご用件をお聞かせください。"

    # 無音や極端に短い場合の促し
    if len(t) <= 2:
        return f"お電話ありがとうございます。{INFO['name']}です。ご用件をお聞かせください。"

    return None
