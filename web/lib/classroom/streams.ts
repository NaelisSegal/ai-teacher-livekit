/**
 * Reads text that someone in the room sent, without trusting its size. Anyone may
 * send on the chat and reaction topics, and a stream can be made enormous, so it is
 * read a piece at a time and dropped once it is longer than it has reason to be.
 * Leaving the loop early tells LiveKit to stop the stream.
 */
export async function readCapped(
  reader: AsyncIterable<string>,
  maxChars: number,
): Promise<string | null> {
  let text = '';
  try {
    for await (const piece of reader) {
      text += piece;
      if (text.length > maxChars) return null;
    }
  } catch {
    return null;
  }
  return text;
}
