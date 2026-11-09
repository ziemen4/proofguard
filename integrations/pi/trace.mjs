export function traceValue(value) {
  return JSON.parse(JSON.stringify(value, (key, item) => {
    if (/^(errorMessage|error)$/i.test(key)) return '[omitted]';
    if (/^(authorization|apiKey|access_token|refresh_token|credentials)$/i.test(key)) return '[omitted]';
    return item;
  }));
}

export function relayEvent(event) {
  // Send snapshots once, then deltas. The guest restores the SDK's partial view.
  const { partial, ...delta } = event;
  if (event.type === 'start') return traceValue(event);
  if (event.type.endsWith('_start') && partial?.content) {
    const { thinkingSignature, ...block } = partial.content[event.contentIndex];
    return traceValue({ ...delta, block });
  }
  return traceValue(delta);
}

export function visibleEvent(event) {
  if (event.type === 'message_update') {
    // Preserve every delta and completed message, without quadratic snapshots.
    const { partial, ...delta } = event.assistantMessageEvent;
    const { message, ...lifecycle } = event;
    return traceValue({ ...lifecycle, assistantMessageEvent: delta });
  }
  return traceValue(event);
}
