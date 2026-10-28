"""Keep task and paired tool calls when a generative history summary is empty."""
import json
from langchain_core.messages import ToolMessage


def compact_messages(messages):
    def shorten(value):
        if isinstance(value,str):return value[:2400]
        if isinstance(value,list):return [shorten(v) for v in value[:6]]
        if isinstance(value,dict):return {k:shorten(v) for k,v in value.items() if k not in ('geometry','collections')}
        return value
    result=[]
    for message in messages:
        if isinstance(message,ToolMessage) and isinstance(message.content,str):
            try:
                data=json.loads(message.content)
                content=json.dumps({'context_excerpt':shorten(data),
                    'note':'Tool text shortened for context. Exact geo values remain accessible by data_ref via read_geo_result.'},ensure_ascii=False)
            except (ValueError,TypeError):content=message.content[:2400]
            message=message.model_copy(update={'content':content})
        result.append(message)
    return result


def summarize_safely(summarizer,state):
    context=state.get('context') or {}
    if context.get('deterministic_summary'):
        return {'summarized_messages':compact_messages(state['messages'])}
    update=summarizer.invoke(state)
    running=update.get('context',{}).get('running_summary')
    summary=running.get('summary') if isinstance(running,dict) else getattr(running,'summary',None)
    if running is not None and (not isinstance(summary,str) or not summary.strip()):
        context={k:v for k,v in context.items() if k!='running_summary'}
        return {'summarized_messages':compact_messages(state['messages']),
                'context':{**context,'deterministic_summary':True}}
    return update
