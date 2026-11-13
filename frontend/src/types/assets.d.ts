// Stylesheet imports are side effects handled by esbuild, not JS exports.
declare module '*.css' {}
declare module 'virtual:shared-lang' {
  const translations: Record<'en' | 'ru', Record<string, string>>;
  export default translations;
  export const errors: Record<'en' | 'ru', Record<string, string>>;
}
