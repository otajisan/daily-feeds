"""モジュール間でやり取りするデータの形。

これまで素の dict で受け渡していた構造を TypedDict で明示する。実体は dict のままなので
実行時の振る舞いは変わらず、キー名の打ち間違いや型の取り違えだけが静的に落ちる。

置き場所を1モジュールにまとめているのは、記事(Item)と設定(Settings)が
config / fetching / scoring / output / cli のほぼ全域で使われるため。
"""

from typing import NotRequired, TypedDict


class Settings(TypedDict):
    """feeds.yml の settings ブロック。load_config が既定値で埋めたあとの形。"""

    retention_days: int
    max_items: int
    json_max_items: int
    score_batch_size: int
    gemini_model: str
    gemini_thinking_level: str | None
    site_url: str
    feed_title: str
    feed_description: str
    user_agent: str


class Feed(TypedDict):
    """購読するフィード1本。name と category は省略できる。"""

    url: str
    name: NotRequired[str]
    category: NotRequired[str]


class Config(TypedDict):
    """feeds.yml 全体。feeds を書き忘れた設定でも動くので必須にしない。"""

    settings: Settings
    feeds: NotRequired[list[Feed]]


class Item(TypedDict):
    """1記事。fetching が組み立て、cli がスコアをマージしてから output へ渡す。

    score / reason は state から合流するまで存在しないので必須にしない。
    """

    id: str
    title: str
    link: str
    summary: str
    published: str
    source: str
    category: str
    score: NotRequired[int]
    reason: NotRequired[str]


class ScoreResult(TypedDict):
    """Geminiの応答から取り出した1記事ぶんのスコア。"""

    score: int
    reason: str


class StateEntry(TypedDict):
    """data/state.json の1エントリ。

    スコアリングに失敗した記事はキャッシュしないので、first_seen だけが
    先に入っているエントリはありえない。逆に古い形式では score だけのこともある。
    """

    score: NotRequired[int]
    reason: NotRequired[str]
    first_seen: NotRequired[str]


class State(TypedDict):
    """data/state.json 全体。"""

    items: dict[str, StateEntry]
