"""S03-07：界面与手动验证用的合成样例。

仓库**不提交二进制 .xlsx**（S02 决定，见 [plan/04](../../../plan/04-测试与质量评估.md)），
因此样例在请求时用 openpyxl 现场生成，经 ``GET /api/samples/{name}`` 下载后即可在
界面里上传。全部为合成数据，不含任何真实问答，零合规风险。

样例覆盖手动验证需要的核心流程与边界：正常记录、五类模拟情境、资料全空、
单条输入失败、批次阻断、S03-06 真实调用用的最长样例、2026-10-06 返工新增的
**技术失败**样例（模拟模式下复现“输出被拒”的界面），以及 S04-07 为批次界面新增的
**部分失败批次**（1 条确定性技术失败 + 2 条正常）与**慢速批次**（整批约 6 秒，
供用户在运行中停掉 worker 制造真实中断）。
"""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO

from openpyxl import Workbook

from ..agent.mock_samples import FORCED_SCENARIOS, SLOW_RECORD_DELAYS_MS
from ..schemas.qa import A_COLUMN, ID_COLUMN, INPUT_COLUMNS, Q_COLUMN, REF_FIELDS
from .reader import PREFERRED_SHEET

#: 强制技术失败样例的记录编号；取自模拟情境的显式映射，避免两处写死而漂移。
FORCED_FAILURE_RECORD_ID = next(iter(FORCED_SCENARIOS))

#: 慢速样例的记录编号；同样取自延迟映射本身（写死编号会让「样例不再慢」这种
#: 漂移变得不可见：改动映射后样例照样能下载，只是不再制造中断窗口）。
SLOW_RECORD_IDS: tuple[str, ...] = tuple(SLOW_RECORD_DELAYS_MS)


@dataclass(frozen=True)
class SampleDefinition:
    """一个可下载样例；``rows`` 已按 ``INPUT_COLUMNS`` 顺序排好。"""

    name: str
    filename: str
    description: str
    rows: tuple[tuple[object, ...], ...]

    @property
    def record_count(self) -> int:
        """非空行数，与预检 ``counts.total`` 同口径（全空白行不计）。"""
        return sum(1 for row in self.rows if _row_has_content(row))


def _row(cells: dict[str, object]) -> tuple[object, ...]:
    return tuple(cells.get(column) for column in INPUT_COLUMNS)


def _row_has_content(row: tuple[object, ...]) -> bool:
    return any(cell is not None and str(cell).strip() for cell in row)


def _qa_row(
    record_id: object,
    q: object = "如何办理校园卡？",
    a: object = "请到校园卡中心办理。",
    refs: tuple[object, ...] = (),
) -> tuple[object, ...]:
    cells: dict[str, object] = {ID_COLUMN: record_id, Q_COLUMN: q, A_COLUMN: a}
    for field, text in zip(REF_FIELDS, refs):
        cells[field] = text
    return _row(cells)


# 编号 1/2/3/6/9 经 crc32 分别落在 correction/wrong/insufficient/correct/forced_review，
# 因此这一份样例在模拟模式下依次判就能看到全部五类结果（由单测守住，防止漂移）。
_FIVE_SCENARIO_ROWS: tuple[tuple[object, ...], ...] = (
    _qa_row(
        "1",
        "如何办理校园卡？",
        "带着身份证到校园卡中心办理。",
        (
            "校园卡首次办理需携带身份证到校园卡中心办理，工本费 20 元。",
            "办理时间为工作日 9:00—17:00。",
        ),
    ),
    _qa_row(
        "2",
        "图书馆开放到几点？",
        "图书馆晚上 22:00 关门。",
        (
            "图书馆开放时间为每日 8:00—21:30，周一上午闭馆整理。",
        ),
    ),
    _qa_row(
        "3",
        "校医院周六上班吗？",
        "周六正常上班。",
        (),
    ),
    _qa_row(
        "6",
        "如何申请宿舍调换？",
        "在后勤系统提交调换申请即可。",
        (
            "宿舍调换需先在后勤系统提交申请，经辅导员与宿管中心审批后办理。",
            "调换申请受理时间为每学期前四周。",
        ),
    ),
    _qa_row(
        "9",
        "补办学生证需要多久？",
        "补办学生证一般三天。",
        (
            "学生证补办在教务大厅受理，制证周期以受理时告知为准。",
        ),
    ),
    _row({}),  # 全空行：计入 skipped_blank_rows，不是记录
)


def _longest_rows() -> tuple[tuple[object, ...], ...]:
    """最长样例：长 q/a 与多行长 ref，用于真实调用与上下文边界的兼容检查。"""
    long_q = (
        "我在 2026 年秋季学期从外地转入本校读大二，之前在原学校已经办理过校园卡，"
        "到校后想同时办校园卡、申请宿舍调换并补办学生证，请问这几件事分别应该去哪里办理、"
        "需要准备哪些材料、各自有没有时间限制？如果材料不齐能不能先办后补？"
    )
    long_a = (
        "校园卡到校园卡中心办理，宿舍调换在后勤系统提交申请，学生证补办去教务大厅；"
        "材料不齐可以先受理后再补交。"
    )
    ref1 = "\n".join(
        f"第 {index} 条：校园卡、宿舍调换与学生证补办分属校园卡中心、宿管中心与教务大厅三个"
        "受理单位，具体窗口与受理时间以各单位当期公告为准；本说明不替代现场告知。"
        for index in range(1, 21)
    )
    ref2 = "宿舍调换申请每学期前四周受理，逾期顺延至下一学期。" * 4
    ref3 = "学生证补办的制证周期与所需材料以教务大厅受理窗口告知为准。" * 4
    return (
        _qa_row("L-1", long_q, long_a, (ref1, ref2, ref3)),
        _qa_row(
            "L-2",
            "校园卡丢了怎么挂失？",
            "在校园卡服务号里挂失。",
            ("校园卡挂失可通过校园卡服务号或校园卡中心窗口办理，挂失后原卡即刻停用。",),
        ),
    )


SAMPLES: tuple[SampleDefinition, ...] = (
    SampleDefinition(
        name="five-scenarios",
        filename="synthetic-five-scenarios.xlsx",
        description="5 条有效记录 + 1 条空行；模拟模式下五条依次落在五类内部情境（三个业务标签 + 复核/更正差异）",
        rows=_FIVE_SCENARIO_ROWS,
    ),
    SampleDefinition(
        name="no-refs",
        filename="synthetic-no-refs.xlsx",
        description="1 条 ref1—ref10 全空的记录（资料不足，仍属有效记录）",
        rows=(_qa_row("1", "校车周五几点发车？", "下午五点。", ()),),
    ),
    SampleDefinition(
        name="partial-errors",
        filename="synthetic-partial-errors.xlsx",
        description="1 条有效记录 + 缺编号/缺 q/缺 a 各 1 条 + 2 条空行（passed 但有条目问题）",
        rows=(
            _qa_row("1", "如何借阅图书？", "凭校园卡在自助机借阅。", ("每证可借 10 册，借期 30 天。",)),
            _qa_row(None, "补办饭卡要多久？", "当场可取。"),
            _qa_row("3", None, "到后勤大厅办理。"),
            _qa_row("4", "校车几点发车？", None),
            _row({}),
            _row({column: "   " for column in INPUT_COLUMNS}),
        ),
    ),
    SampleDefinition(
        name="blocked",
        filename="synthetic-blocked.xlsx",
        description="没有任何有效记录（全部缺 q 或空行）：预检为 blocked，不调用模型",
        rows=(_qa_row("1", None, "到校园卡中心办理。"), _row({})),
    ),
    SampleDefinition(
        name="longest",
        filename="synthetic-longest.xlsx",
        description="最长样例：长 q/a 与 20 行长资料，用于真实调用与上下文边界检查",
        rows=_longest_rows(),
    ),
    SampleDefinition(
        name="mixed-outcome",
        filename="synthetic-mixed-outcome.xlsx",
        description=(
            "3 条有效记录：编号 FF-1 在模拟模式下确定性技术失败，另 2 条正常判定；"
            "用于验证 partial_failed 批次、失败摘要与「重试失败项」（重试≠必成功）"
        ),
        rows=(
            _qa_row(
                FORCED_FAILURE_RECORD_ID,
                "校园网密码怎么重置？",
                "在自助服务里重置。",
                ("校园网密码可在自助服务终端重置，需刷校园卡并输入原密码。",),
            ),
            _qa_row(
                "M-1",
                "如何办理校园卡？",
                "带身份证到校园卡中心办理。",
                ("校园卡首次办理需携带身份证到校园卡中心办理，工本费 20 元。",),
            ),
            _qa_row(
                "M-2",
                "图书馆开放到几点？",
                "晚上 22:00 关门。",
                ("图书馆开放时间为每日 8:00—21:30，周一上午闭馆整理。",),
            ),
        ),
    ),
    SampleDefinition(
        name="slow-batch",
        filename="synthetic-slow-batch.xlsx",
        description=(
            "3 条有效记录，编号取自慢速映射：模拟模式下每次调用各等待约 1 秒，"
            "整批约 6 秒；用于在运行中停掉 worker 制造真实中断"
        ),
        rows=tuple(
            _qa_row(
                record_id,
                f"第 {index} 条：如何办理校园卡？",
                "带身份证到校园卡中心办理。",
                ("校园卡首次办理需携带身份证到校园卡中心办理，工本费 20 元。",),
            )
            for index, record_id in enumerate(SLOW_RECORD_IDS, start=1)
        ),
    ),
    SampleDefinition(
        name="forced-failure",
        filename="synthetic-forced-failure.xlsx",
        description="1 条编号 FF-1 的记录：模拟模式下刻意让阶段二输出被校验拒绝，用于验证技术失败界面（不产生标签）",
        rows=(
            _qa_row(
                FORCED_FAILURE_RECORD_ID,
                "校园网密码怎么重置？",
                "在自助服务里重置。",
                ("校园网密码可在自助服务终端重置，需刷校园卡并输入原密码。",),
            ),
        ),
    ),
)

_BY_NAME: dict[str, SampleDefinition] = {sample.name: sample for sample in SAMPLES}


def get_sample(name: str) -> SampleDefinition | None:
    return _BY_NAME.get(name)


def build_workbook_bytes(sample: SampleDefinition) -> bytes:
    """在内存中生成工作簿字节；表名固定为 ``QA_REF``，与样例预期一致。

    **不承诺字节稳定**：xlsx 内含生成时间戳，同一份样例两次生成的 sha256 不同。
    界面与手动指南因此只核对**本次上传**的 sha256（上传区显示值应与预检返回的
    ``file_sha256`` 一致），不给出跨次固定的期望值。
    """
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = PREFERRED_SHEET
    sheet.append(list(INPUT_COLUMNS))
    for row in sample.rows:
        sheet.append(list(row))

    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


__all__ = [
    "FORCED_FAILURE_RECORD_ID",
    "SAMPLES",
    "SLOW_RECORD_IDS",
    "SampleDefinition",
    "build_workbook_bytes",
    "get_sample",
]
