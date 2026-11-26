// Sends a file from disk. Server side only.
//
// A video is asked for in pieces (a Range request), so that a browser can start it
// part-way through and jump about in it. Pictures are sent whole.

import { createReadStream } from 'node:fs';
import { stat } from 'node:fs/promises';
import path from 'node:path';
import { Readable } from 'node:stream';

export const PICTURES: Record<string, string> = {
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.webp': 'image/webp',
  '.gif': 'image/gif',
};

export const VIDEOS: Record<string, string> = {
  '.mp4': 'video/mp4',
  '.m4v': 'video/mp4',
  '.mov': 'video/quicktime',
  '.webm': 'video/webm',
};

const notFound = () => new Response('Not found', { status: 404 });

/** The parts of an address as plain names: no dot at the start, so no "..", and no
 *  slashes. null if any part is not one. */
export function plainParts(parts: string[], most = 4): string[] | null {
  const ok = parts.length > 0 && parts.length <= most && parts.every((p) => /^\w[\w.-]*$/.test(p));
  return ok ? parts : null;
}

export function typeOf(file: string, types: Record<string, string>): string | undefined {
  return types[path.extname(file).toLowerCase()];
}

export async function serveFile(request: Request, file: string, type: string): Promise<Response> {
  const size = await stat(file).then(
    (s) => (s.isFile() ? s.size : -1),
    () => -1,
  );
  if (size < 0) return notFound();
  const headers: Record<string, string> = {
    'Content-Type': type,
    'X-Content-Type-Options': 'nosniff',
    // An SVG opened on its own could run script. This stops that.
    'Content-Security-Policy': "default-src 'none'; style-src 'unsafe-inline'; sandbox",
    'Cache-Control': 'public, max-age=300',
    'Accept-Ranges': 'bytes',
  };
  const range = /^bytes=(\d*)-(\d*)$/.exec(request.headers.get('range') ?? '');
  let start = 0;
  let end = size - 1;
  let status = 200;
  if (range && (range[1] || range[2]) && size > 0) {
    if (range[1]) {
      start = Number(range[1]);
      if (range[2]) end = Math.min(Number(range[2]), size - 1);
    } else {
      start = Math.max(0, size - Number(range[2])); // the last so-many bytes
    }
    if (start > end || start >= size) {
      return new Response(null, { status: 416, headers: { 'Content-Range': `bytes */${size}` } });
    }
    status = 206;
    headers['Content-Range'] = `bytes ${start}-${end}/${size}`;
  }
  headers['Content-Length'] = String(size === 0 ? 0 : end - start + 1);
  if (size === 0) return new Response(null, { status, headers });
  const body = Readable.toWeb(createReadStream(file, { start, end })) as ReadableStream;
  return new Response(body, { status, headers });
}
