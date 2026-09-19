import pytest

from test_macd_j_selection import source, with_macd_spreads
from test_macd_j_v4 import enriched
from tradex.stock_selection.macd_j import screen_macd_j
from tradex.stock_selection.macd_rules import v4_signal
from tradex.stock_selection.contracts import MacdJScreenV1


@pytest.mark.parametrize("js,gap", [
    ((50,40,30,25,10,18),0), ((50,40,30,10,18,20),1),
    ((50,40,10,18,20,22),2), ((50,10,18,20,22,24),None)])
@pytest.mark.parametrize("pending", [False,True])
def test_v5_only_expands_j_lead_to_two_sessions(js,gap,pending):
    data=source(js)
    if pending:
        days=tuple(day.model_copy(update={"points":(day.points[0].model_copy(
            update={"dif":day.points[0].dea+spread}),)})
            for day,spread in zip(data.technicals.days,[-.9,-.8,-.7,-.5,-.3,-.1]))
        data=data.model_copy(update={"technicals":data.technicals.model_copy(update={"days":days})})
    data=enriched(data,high=100)
    new=screen_macd_j(data)
    old=screen_macd_j(data,version="v4")
    selected=new.pending_candidates if pending else new.candidates
    assert len(selected)==(gap is not None)
    assert len(old.pending_candidates if pending else old.candidates)==(gap in (0,1))
    if selected:
        assert selected[0].gap_sessions==gap
        assert selected[0].signal_group==("same_day" if gap==0 else "prior_2_sessions")
    assert MacdJScreenV1.model_validate_json(new.model_dump_json())==new
    assert MacdJScreenV1.model_validate_json(old.model_dump_json())==old


def test_old_macd_cross_and_excess_volume_are_still_rejected():
    oldcross=enriched(with_macd_spreads([-.1,-.1,-.1,-.1,.1,.2]),high=100)
    assert screen_macd_j(oldcross).matched_count==0
    data=enriched(source((50,40,10,18,20,22)),high=100,ratio=1.5001)
    assert screen_macd_j(data).matched_count==0
    assert screen_macd_j(data).excluded_counts=={"volume_ratio_above_1_5":1}


def test_prior_two_turn_still_requires_today_rising_and_exact_version_evidence():
    data=enriched(source((50,40,10,18,22,21)),high=100)
    assert screen_macd_j(data).matched_count==0
    raw=screen_macd_j(enriched(source((50,40,10,18,20,22)),high=100)).model_dump(mode="json")
    raw['screen_version']='macd-j-upturn-main-board.v4'
    with pytest.raises(ValueError):MacdJScreenV1.model_validate(raw)
