"""Regression for HCA session: Dutch action announcements must continue the turn."""
import pytest
from agent.agent_runtime_helpers import trailing_continue_intent

@pytest.mark.parametrize('text', [
    'Ik ga nu echt door: schermopname van het Bot Screen, ticketschema en een dubbelcheck op bestaande tickets, alles tegelijk.',
    'Nu doe ik de schermopname en haal het ticketschema op, allebei tegelijk.',
    'Top, ik controleer nu in het Bot Screen of je echt bent ingelogd. Ondertussen haal ik het ticketschema op.',
    'Ik maak het ticket nu aan. Eerst haal ik het schema op en controleer ik op dubbele tickets. Tegelijk doe ik de schermopname van het Bot Screen.',
    'Sorry, ik was gestopt voor de capture. Ik pak hem nu.',
    'Ik haal de ticketprocedure op en maak tegelijk de schermopname die nog openstond.',
])
def test_dutch_announced_action_is_not_a_final(text):
    assert trailing_continue_intent(text)

@pytest.mark.parametrize('text', [
    'Ik heb het ticket aangemaakt. Het nummer is 123.',
    'Ik controleer dit morgen zodra je toestemming geeft.',
    'Ik kan het schema ophalen als je dat wilt.',
    'Zal ik de schermopname maken?',
    'Ik wacht op jouw bevestiging.',
    'Ik kan niet doorgaan: toegang geweigerd.',
    'Ik controleer nu het bestand. Het bevat drie regels.',
])
def test_dutch_answer_or_user_gate_is_not_retried(text):
    assert not trailing_continue_intent(text)

from tests.agent.test_reasoning_only_stop_persistence import loop_agent, _run

def test_dutch_stall_reprompts_in_real_conversation_loop(loop_agent):
    from tests.agent.test_run_agent import _mock_response
    loop_agent.valid_tool_names = {'terminal'}
    loop_agent._stall_guards = True
    result = _run(loop_agent, [
        _mock_response(content='Nu doe ik de schermopname en haal het ticketschema op, allebei tegelijk.', finish_reason='stop'),
        _mock_response(content='De controle is afgerond.', finish_reason='stop'),
    ], user_message='Controleer het scherm.')
    assert result['api_calls'] == 2
    assert result['final_response'] == 'De controle is afgerond.'

def test_dutch_stall_retains_retry_bound(loop_agent):
    from tests.agent.test_run_agent import _mock_response
    loop_agent.valid_tool_names = {'terminal'}
    loop_agent._stall_guards = True
    result = _run(loop_agent, [
        _mock_response(content='Ik pak hem nu.', finish_reason='stop') for _ in range(3)
    ], user_message='Controleer het scherm.')
    assert result['api_calls'] == 3

@pytest.mark.parametrize('mode,tools,choice,expect', [
    ('chat_completions',[{'type':'function'}],None,'required'),
    ('chat_completions',[],None,None),
    ('chat_completions',[{'type':'function'}],'none','none'),
    ('anthropic',[{'type':'function'}],None,None),
])
def test_stall_requires_tool_only_for_eligible_retry(mode,tools,choice,expect):
    from types import SimpleNamespace
    from agent.turn_api_request import _apply_action_stall_tool_choice
    kw={'tools':tools}
    if choice is not None: kw['tool_choice']=choice
    _apply_action_stall_tool_choice(SimpleNamespace(api_mode=mode),kw,[{'role':'user','_action_stall_retry':True}])
    assert kw.get('tool_choice') == expect

def test_stall_tool_requirement_does_not_leak_after_tool_result():
    from types import SimpleNamespace
    from agent.turn_api_request import _apply_action_stall_tool_choice
    kw={'tools':[{'type':'function'}]}
    _apply_action_stall_tool_choice(SimpleNamespace(api_mode='chat_completions'),kw,[{'role':'user','_action_stall_retry':True},{'role':'tool','content':'done'}])
    assert 'tool_choice' not in kw

def test_exhausted_stall_is_explicit_failed_turn(loop_agent):
    from tests.agent.test_run_agent import _mock_response
    loop_agent.valid_tool_names={'terminal'}
    loop_agent._stall_guards=True
    result=_run(loop_agent,[_mock_response(content='Ik pak hem nu.',finish_reason='stop') for _ in range(3)])
    assert result.get('failure_reason') == 'invalid_response'
    assert result['final_response'] and result['final_response'] != 'Ik pak hem nu.'


def test_required_tool_choice_is_wired_into_conversation_request(loop_agent):
    from tests.agent.test_run_agent import _mock_response
    loop_agent.valid_tool_names = {'terminal'}
    loop_agent.tools = [{'type':'function','function':{'name':'terminal','parameters':{'type':'object','properties':{}}}}]
    loop_agent._stall_guards = True
    _run(loop_agent, [
        _mock_response(content='Ik pak hem nu.', finish_reason='stop'),
        _mock_response(content='De controle is afgerond.', finish_reason='stop'),
    ])
    calls=loop_agent.client.chat.completions.create.call_args_list
    assert calls[0].kwargs.get('tool_choice') != 'required'
    assert calls[1].kwargs['tool_choice'] == 'required'
    assert all('_action_stall_retry' not in row for row in calls[1].kwargs['messages'])
