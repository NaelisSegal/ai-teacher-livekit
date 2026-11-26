// Serves the pictures and videos that lessons use, from the lessons folders.
// Only pictures and videos, and only from inside those folders.

import { findLessonFile } from '@/lib/lessons';
import { PICTURES, plainParts, serveFile, typeOf, VIDEOS } from '@/lib/serveFile';
import { NextRequest } from 'next/server';

export async function GET(request: NextRequest, context: { params: Promise<{ path: string[] }> }) {
  const parts = plainParts((await context.params).path);
  const type = parts ? typeOf(parts[parts.length - 1], { ...PICTURES, ...VIDEOS }) : undefined;
  const file = parts && type ? await findLessonFile(...parts) : null;
  if (!file || !type) return new Response('Not found', { status: 404 });
  return serveFile(request, file, type);
}
