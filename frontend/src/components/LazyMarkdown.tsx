// React 19 removed the global `JSX` namespace that React 18's types declared ambiently, so
// the `JSX.Element` return type below must now be imported explicitly from "react".
import { lazy, Suspense, type ComponentType, type JSX, type ReactNode } from "react";

// Single shared dynamic-import boundary for react-markdown + remark-gfm so they
// only load when something actually renders markdown. Without this every panel
// that imports react-markdown ships it eagerly in its route chunk.
type ReactMarkdownLib = typeof import("react-markdown");
type ReactMarkdownProps = Parameters<ReactMarkdownLib["default"]>[0];

const ReactMarkdown = lazy<ComponentType<ReactMarkdownProps>>(async () => {
  const [{ default: RM }, { default: gfm }] = await Promise.all([
    import("react-markdown"),
    import("remark-gfm"),
  ]);
  const Wrapped: ComponentType<ReactMarkdownProps> = (props) => {
    const remarkPlugins = props.remarkPlugins ?? [gfm];
    return <RM {...props} remarkPlugins={remarkPlugins} />;
  };
  return { default: Wrapped };
});

export function Markdown(props: ReactMarkdownProps & { fallback?: ReactNode }): JSX.Element {
  const { fallback, ...rest } = props;
  return (
    <Suspense fallback={fallback ?? null}>
      <ReactMarkdown {...rest} />
    </Suspense>
  );
}
