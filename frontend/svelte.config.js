import adapter from '@sveltejs/adapter-static';

/** @type {import('@sveltejs/kit').Config} */
const config = {
  kit: {
    adapter: adapter({
      pages: '../dashboards',
      assets: '../dashboards',
      fallback: 'index.html',
      precompress: false,
    }),
    alias: { $lib: 'src/lib' },
  },
};

export default config;
