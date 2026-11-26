import Link from 'next/link';
import { listLessons } from '@/lib/lessons';
import { OPEN_ROOMS } from '@/lib/schedule';
import { SITE_NAME } from '@/lib/site';

// A plain list for development: every lesson found in the lessons folders.
// Every visit makes new links, so each one starts a class from the beginning.
export const dynamic = 'force-dynamic';

function freshSuffix(): string {
  return Math.random().toString(36).slice(2, 6);
}

// Development only, like this page: the helper code, so the links work as they are.
const HELPER_CODE = encodeURIComponent(process.env.CH_HELPER_CODE ?? '');

export default async function Page() {
  // On a real server there is no list of every lesson, with links for helpers. Each
  // class has its own link, /class/<room>, which whoever runs the classes gives out.
  if (!OPEN_ROOMS) {
    return (
      <main className="ch-join">
        <div>
          <h1>{SITE_NAME}</h1>
          <p>Classes are joined from the link for each one.</p>
        </div>
      </main>
    );
  }
  const fresh = freshSuffix();
  const lessons = await listLessons();
  return (
    <main className="ch-join">
      <div>
        <h1>{SITE_NAME}</h1>
        <p>
          Development build. Each link starts a new class from the beginning, a few seconds after
          you join.
        </p>
        {lessons.length === 0 ? (
          <p>
            There are no lessons yet. A lesson is a script,{' '}
            <code>lessons/&lt;course&gt;/&lt;lesson&gt;.seg</code>.
          </p>
        ) : (
          <ul>
            {lessons.map((lesson) => {
              // A room name is letters, numbers and hyphens, 64 at most.
              const room = `${lesson.name.replace('/', '-').slice(0, 50)}-${fresh}`;
              const query = `lesson=${lesson.name}`;
              return (
                <li key={lesson.name}>
                  <Link href={`/class/${room}?${query}`}>{lesson.title}</Link> (
                  <code>{lesson.name}</code>
                  {lesson.minutes ? `, about ${lesson.minutes} minutes` : ''}){' '}
                  <Link href={`/class/${room}?${query}&as=helper&code=${HELPER_CODE}`}>
                    as the helper
                  </Link>
                  {lesson.parts ? (
                    <>
                      {' '}
                      <Link href={`/class/live-${room}?${query}`}>
                        taught live (needs a model key)
                      </Link>
                    </>
                  ) : null}
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </main>
  );
}
