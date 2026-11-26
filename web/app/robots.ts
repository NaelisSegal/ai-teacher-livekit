import type { MetadataRoute } from 'next';

// Asks search engines and other well-behaved crawlers to stay out of classes.
// It does not stop a bot that ignores it. Joining a class sends for a lecturer,
// so nothing automatic should wander in.
export default function robots(): MetadataRoute.Robots {
  return {
    rules: [
      {
        userAgent: '*',
        allow: '/',
        disallow: ['/class/', '/api/', '/lesson-files/', '/class-files/'],
      },
    ],
  };
}
