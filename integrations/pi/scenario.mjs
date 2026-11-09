/** Deterministic provider for offline SDK fixtures, never model-security evidence. */
import { LIMITS, boundedText, keys } from './config.mjs';
import { loadPiAi } from './sdk.mjs';
import { errorMessage } from './transport.mjs';

export function validateScenario(value) {
  if (!keys(value, ['schema', 'turns']) || value.schema !== 1 || !Array.isArray(value.turns) ||
      !value.turns.length || value.turns.length > LIMITS.turns) throw new Error('invalid offline scenario');
  for (const turn of value.turns) {
    if (keys(turn, ['text']) && boundedText(turn.text, LIMITS.answer)) continue;
    if (!keys(turn, ['toolCalls']) || !Array.isArray(turn.toolCalls) || !turn.toolCalls.length || turn.toolCalls.length > 8) {
      throw new Error('invalid offline turn');
    }
    for (const call of turn.toolCalls) {
      if (!keys(call, ['name', 'arguments']) || !['read', 'write_program'].includes(call.name) ||
          !(call.name === 'write_program' ? keys(call.arguments, ['program']) && boundedText(call.arguments.program, LIMITS.program)
            : keys(call.arguments, ['path']) && boundedText(call.arguments.path, 4096))) throw new Error('invalid offline tool call');
    }
  }
  return value;
}

export async function createScenarioProvider(value) {
  const scenario = validateScenario(structuredClone(value));
  const { createAssistantMessageEventStream } = await loadPiAi();
  let index = 0;
  return (model, _context, options = {}) => {
    const stream = createAssistantMessageEventStream();
    const turn = scenario.turns[index++];
    const message = { ...errorMessage(model), errorMessage: undefined, stopReason: 'pending', content: [] };
    const emit = event => stream.push(structuredClone(event));
    if (!turn || options.signal?.aborted) {
      emit({ type: 'error', reason: 'error', error: errorMessage(model, 'offline scenario exhausted or aborted') });
      stream.end();
      return stream;
    }
    emit({ type: 'start', partial: message });
    if (turn.text !== undefined) {
      message.content.push({ type: 'text', text: '' });
      emit({ type: 'text_start', contentIndex: 0, partial: message });
      message.content[0].text = turn.text;
      emit({ type: 'text_delta', contentIndex: 0, delta: turn.text, partial: message });
      emit({ type: 'text_end', contentIndex: 0, content: turn.text, partial: message });
      message.stopReason = 'stop';
    } else {
      turn.toolCalls.forEach((call, contentIndex) => {
        const toolCall = { type: 'toolCall', id: `offline-${index}-${contentIndex + 1}`, name: call.name, arguments: {} };
        message.content.push(toolCall);
        emit({ type: 'toolcall_start', contentIndex, partial: message });
        toolCall.arguments = call.arguments;
        emit({ type: 'toolcall_delta', contentIndex, delta: JSON.stringify(call.arguments), partial: message });
        emit({ type: 'toolcall_end', contentIndex, toolCall, partial: message });
      });
      message.stopReason = 'toolUse';
    }
    emit({ type: 'done', reason: message.stopReason, message });
    stream.end();
    return stream;
  };
}
