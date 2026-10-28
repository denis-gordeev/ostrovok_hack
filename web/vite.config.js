import {defineConfig} from 'vite';
export default defineConfig({base: '/ui/', server: {proxy: {
  '/threads': 'http://127.0.0.1:2024', '/ui/tiles': 'http://127.0.0.1:2024'
}}});
