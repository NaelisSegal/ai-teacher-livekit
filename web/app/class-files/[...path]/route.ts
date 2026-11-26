// Serves the pictures made during a class by the image model.
//
// The lecturer saves them in one folder per class (agent/src/lecturer/room/pictures.py)
// and this sends them to browsers. So the lecturer and the web app must be able to
// reach the same folder: generated/ at the top of the repo, or CH_GENERATED_DIR.

import { PICTURES, plainParts, serveFile, typeOf } from '@/lib/serveFile';
import { NextRequest } from 'next/server';
import path from 'node:path';

const GENERATED_DIR = path.resolve(
  process.env.CH_GENERATED_DIR || path.join(process.cwd(), '..', 'generated'),
);

export async function GET(request: NextRequest, context: { params: Promise<{ path: string[] }> }) {
  // <room>/<file>, and nothing deeper.
  const parts = plainParts((await context.params).path, 2);
  const type = parts?.length === 2 ? typeOf(parts[1], PICTURES) : undefined;
  if (!parts || !type) return new Response('Not found', { status: 404 });
  const file = path.resolve(GENERATED_DIR, ...parts);
  if (!file.startsWith(GENERATED_DIR + path.sep)) return new Response('Not found', { status: 404 });
  return serveFile(request, file, type);
}
