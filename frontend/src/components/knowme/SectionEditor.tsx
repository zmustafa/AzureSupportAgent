// Section editor — a true WYSIWYG (TipTap/ProseMirror) for a Know-Me section's prose, with
// a Markdown fallback tab, plus one-click insert of the architecture diagram, a custom
// Mermaid block, or an uploaded/pasted image. Round-trips Markdown so storage, AI re-gen,
// revisions and PDF export all stay Markdown. Asset image refs (asset:ID) are rewritten to
// the live API URL for editing and back to asset:ID on save.
import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from "react";
import { useEditor, EditorContent } from "@tiptap/react";
import StarterKit from "@tiptap/starter-kit";
import { Table } from "@tiptap/extension-table";
import { TableRow } from "@tiptap/extension-table-row";
import { TableHeader } from "@tiptap/extension-table-header";
import { TableCell } from "@tiptap/extension-table-cell";
import { Markdown as TiptapMarkdown } from "tiptap-markdown";
import { api, type KnowMeSection } from "../../api";
import { formatError } from "../../utils/format";
import { Markdown } from "../LazyMarkdown";
import { MermaidDiagram } from "../MermaidDiagram";
import { ResizableImage } from "./ResizableImage";

function assetToUrl(architectureId: string, md: string): string {
  return (md || "").replace(/asset:([0-9a-fA-F-]{36})/g, (_m, id) => api.knowMeAssetUrl(architectureId, id));
}
function urlToAsset(architectureId: string, md: string): string {
  const base = api.knowMeAssetUrl(architectureId, "").replace(/\/$/, "");
  const re = new RegExp(base.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + "/([0-9a-fA-F-]{36})", "g");
  return (md || "").replace(re, (_m, id) => `asset:${id}`);
}

/** Read the document as Markdown from the tiptap-markdown storage (not in TS types). */
function getEditorMarkdown(editor: { storage: unknown }): string {
  const storage = editor.storage as { markdown?: { getMarkdown?: () => string } };
  return storage.markdown?.getMarkdown?.() ?? "";
}

// Render ```mermaid fenced blocks as live diagrams in the Preview tab (same renderer as the
// document + chat), and honor the ``?w=`` width hint resized images carry in their src.
const previewComponents = {
  code({ className, children, ...props }: { className?: string; children?: React.ReactNode }) {
    const text = String(Array.isArray(children) ? children.join("") : (children ?? ""));
    if (/\blanguage-mermaid\b/.test(className || "") && text.trim()) {
      return <MermaidDiagram code={text.replace(/\n$/, "")} />;
    }
    return <code className={className} {...props}>{children}</code>;
  },
  img({ src, alt, ...props }: { src?: string; alt?: string }) {
    const m = /[?&]w=(\d+)/.exec(src || "");
    const width = m ? `${m[1]}px` : undefined;
    const cleanSrc = (src || "").replace(/[?&]w=\d+/g, "").replace(/\?$/, "");
    return <img src={cleanSrc} alt={alt || ""} style={width ? { width, maxWidth: "100%" } : undefined} {...props} />;
  },
};

const TBtn = ({ on, active, title, children }: { on: () => void; active?: boolean; title: string; children: React.ReactNode }) => (
  <button
    type="button"
    title={title}
    onMouseDown={(e) => e.preventDefault()}
    onClick={on}
    className={`rounded px-2 py-1 text-xs font-medium ${active ? "bg-brand/10 text-brand" : "text-gray-600 hover:bg-gray-100"}`}
  >
    {children}
  </button>
);

export function SectionEditor({
  architectureId,
  section,
  onClose,
  onSaved,
}: {
  architectureId: string;
  section: KnowMeSection;
  onClose: () => void;
  onSaved: (sectionKey: string, content: string) => Promise<void> | void;
}) {
  const [mode, setMode] = useState<"visual" | "markdown" | "preview">("visual");
  const [mdText, setMdText] = useState(section.content || "");
  const [previewMd, setPreviewMd] = useState("");
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState("");
  // Bumped on every editor selection/transaction so the toolbar's context-sensitive buttons
  // (active marks, in-table row/col controls) re-render with the current cursor.
  const [, forceToolbar] = useReducer((n: number) => n + 1, 0);
  const fileRef = useRef<HTMLInputElement>(null);
  const initialMd = useMemo(() => assetToUrl(architectureId, section.content || ""), [architectureId, section.content]);

  const editor = useEditor({
    extensions: [
      StarterKit.configure({ heading: { levels: [2, 3, 4] } }),
      ResizableImage.configure({ inline: false, allowBase64: true }),
      Table.configure({ resizable: true }),
      TableRow,
      TableHeader,
      TableCell,
      TiptapMarkdown.configure({ html: false, tightLists: true, transformPastedText: true }),
    ],
    content: initialMd,
    onSelectionUpdate: forceToolbar,
    onTransaction: forceToolbar,
  });

  // Keep the three modes in sync when switching. ``prevMode`` lets each mode pull the latest
  // content from whichever editor was last active so no edits are lost across tab switches.
  const prevModeRef = useRef<"visual" | "markdown" | "preview">("visual");
  useEffect(() => {
    if (!editor) return;
    const from = prevModeRef.current;
    if (mode === "markdown" && from === "visual") {
      // Visual → Markdown: serialize the editor into the textarea.
      setMdText(urlToAsset(architectureId, getEditorMarkdown(editor)));
    } else if (mode === "visual" && from === "markdown") {
      // Markdown → Visual: parse the textarea back into the editor (tiptap-markdown parses
      // a Markdown string passed to setContent), with asset refs resolved to live URLs.
      editor.commands.setContent(assetToUrl(architectureId, mdText));
    } else if (mode === "preview") {
      const src = from === "markdown" ? mdText : urlToAsset(architectureId, getEditorMarkdown(editor));
      setPreviewMd(assetToUrl(architectureId, src)); // resolve asset refs → live URLs for render
    }
    prevModeRef.current = mode;
  }, [mode]); // eslint-disable-line react-hooks/exhaustive-deps

  const currentMarkdown = useCallback((): string => {
    if (mode === "markdown") return mdText;
    if (editor) return urlToAsset(architectureId, getEditorMarkdown(editor));
    return mdText;
  }, [mode, mdText, editor, architectureId]);

  const insertMarkdown = useCallback(
    (snippet: string) => {
      setMdText((t) => `${t}\n\n${snippet}\n`);
    },
    [],
  );

  // Insert a fenced code block as a real node in the visual editor (so it round-trips as a
  // ```lang fence), or append the fence in the markdown tab.
  const insertCodeBlock = useCallback(
    (lang: string, source: string) => {
      if (mode === "markdown") {
        insertMarkdown("```" + lang + "\n" + source + "\n```");
      } else if (editor) {
        editor
          .chain()
          .focus()
          .insertContent({ type: "codeBlock", attrs: { language: lang }, content: [{ type: "text", text: source }] })
          .run();
      }
    },
    [mode, editor, insertMarkdown],
  );

  async function insertArchitectureDiagram() {
    setErr("");
    try {
      const { mermaid } = await api.knowMeMermaid(architectureId);
      insertCodeBlock("mermaid", mermaid);
    } catch (e) {
      setErr(formatError(e));
    }
  }

  function insertMermaidTemplate() {
    insertCodeBlock("mermaid", "flowchart TD\n  A[Client] --> B[Service]\n  B --> C[(Database)]");
  }

  function insertImageAsset(assetId: string, filename: string) {
    if (mode === "markdown") {
      insertMarkdown(`![${filename}](asset:${assetId})`);
    } else if (editor) {
      // A real image node → immediately selectable + resizable via the drag handle.
      editor.chain().focus().setImage({ src: api.knowMeAssetUrl(architectureId, assetId), alt: filename }).run();
    }
  }

  async function onPickImage(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file) return;
    setErr("");
    try {
      const { asset } = await api.uploadKnowMeAsset(architectureId, file);
      insertImageAsset(asset.id, asset.filename);
    } catch (err2) {
      setErr(formatError(err2));
    }
  }

  // Paste-an-image support in visual mode.
  useEffect(() => {
    if (!editor) return;
    const dom = editor.view.dom;
    const onPaste = async (e: ClipboardEvent) => {
      const item = Array.from(e.clipboardData?.items || []).find((it) => it.type.startsWith("image/"));
      if (!item) return;
      const file = item.getAsFile();
      if (!file) return;
      e.preventDefault();
      try {
        const { asset } = await api.uploadKnowMeAsset(architectureId, file);
        editor.chain().focus().setImage({ src: api.knowMeAssetUrl(architectureId, asset.id), alt: asset.filename }).run();
      } catch (err2) {
        setErr(formatError(err2));
      }
    };
    dom.addEventListener("paste", onPaste as unknown as EventListener);
    return () => dom.removeEventListener("paste", onPaste as unknown as EventListener);
  }, [editor, architectureId]);

  async function save() {
    setSaving(true);
    setErr("");
    try {
      await onSaved(section.key, currentMarkdown());
      onClose();
    } catch (e) {
      setErr(formatError(e));
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4" onClick={() => !saving && onClose()}>
      <div className="flex max-h-[88vh] w-full max-w-3xl flex-col overflow-hidden rounded-2xl bg-white shadow-2xl" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center gap-2 border-b px-4 py-3">
          <span className="text-sm font-semibold text-gray-800">✏️ Edit — {section.label}</span>
          <div className="ml-3 flex rounded-lg border p-0.5 text-xs">
            <button onClick={() => setMode("visual")} className={`rounded px-2 py-0.5 ${mode === "visual" ? "bg-brand/10 text-brand" : "text-gray-500"}`}>Visual</button>
            <button onClick={() => setMode("markdown")} className={`rounded px-2 py-0.5 ${mode === "markdown" ? "bg-brand/10 text-brand" : "text-gray-500"}`}>Markdown</button>
            <button onClick={() => setMode("preview")} className={`rounded px-2 py-0.5 ${mode === "preview" ? "bg-brand/10 text-brand" : "text-gray-500"}`}>Preview</button>
          </div>
          <button onClick={() => !saving && onClose()} className="ml-auto rounded-md px-2 py-1 text-gray-400 hover:bg-gray-100">✕</button>
        </div>

        {/* Toolbar */}
        <div className="flex flex-wrap items-center gap-1 border-b bg-gray-50/60 px-3 py-1.5">
          {mode === "visual" && editor && (
            <>
              <TBtn title="Bold" active={editor.isActive("bold")} on={() => editor.chain().focus().toggleBold().run()}><b>B</b></TBtn>
              <TBtn title="Italic" active={editor.isActive("italic")} on={() => editor.chain().focus().toggleItalic().run()}><i>I</i></TBtn>
              <TBtn title="Heading" active={editor.isActive("heading", { level: 3 })} on={() => editor.chain().focus().toggleHeading({ level: 3 }).run()}>H</TBtn>
              <TBtn title="Bullet list" active={editor.isActive("bulletList")} on={() => editor.chain().focus().toggleBulletList().run()}>• List</TBtn>
              <TBtn title="Numbered list" active={editor.isActive("orderedList")} on={() => editor.chain().focus().toggleOrderedList().run()}>1. List</TBtn>
              <TBtn title="Code block" active={editor.isActive("codeBlock")} on={() => editor.chain().focus().toggleCodeBlock().run()}>{"</>"}</TBtn>
              <span className="mx-1 h-4 w-px bg-gray-200" />
              {editor.isActive("table") ? (
                <>
                  <TBtn title="Add column" on={() => editor.chain().focus().addColumnAfter().run()}>＋Col</TBtn>
                  <TBtn title="Add row" on={() => editor.chain().focus().addRowAfter().run()}>＋Row</TBtn>
                  <TBtn title="Delete column" on={() => editor.chain().focus().deleteColumn().run()}>－Col</TBtn>
                  <TBtn title="Delete row" on={() => editor.chain().focus().deleteRow().run()}>－Row</TBtn>
                  <TBtn title="Toggle header row" on={() => editor.chain().focus().toggleHeaderRow().run()}>Hdr</TBtn>
                  <TBtn title="Delete table" on={() => editor.chain().focus().deleteTable().run()}>🗑 Table</TBtn>
                </>
              ) : (
                <TBtn title="Insert a 3×3 table" on={() => editor.chain().focus().insertTable({ rows: 3, cols: 3, withHeaderRow: true }).run()}>▦ Table</TBtn>
              )}
              <span className="mx-1 h-4 w-px bg-gray-200" />
            </>
          )}
          {mode === "visual" && (
            <>
              <TBtn title="Insert the architecture diagram as Mermaid" on={() => void insertArchitectureDiagram()}>📐 Diagram</TBtn>
              <TBtn title="Insert a Mermaid block" on={insertMermaidTemplate}>✎ Mermaid</TBtn>
              <TBtn title="Insert an image (or paste one directly)" on={() => fileRef.current?.click()}>🖼️ Image</TBtn>
            </>
          )}
          {mode === "markdown" && (
            <span className="px-1 text-[11px] text-gray-400">Editing raw Markdown — tables use | pipes | syntax.</span>
          )}
          {mode === "preview" && (
            <span className="px-1 text-[11px] text-gray-400">Read-only preview — diagrams &amp; images render as in the document.</span>
          )}
          <input ref={fileRef} type="file" accept="image/*" hidden onChange={onPickImage} />
        </div>

        {/* Body */}
        <div className="min-h-0 flex-1 overflow-auto p-4">
          {mode === "visual" ? (
            <EditorContent
              editor={editor}
              className="prose prose-sm max-w-none [&_.ProseMirror]:min-h-[18rem] [&_.ProseMirror]:outline-none [&_img]:max-w-full [&_img]:rounded-lg [&_table]:w-full [&_table]:border-collapse [&_table]:text-[12px] [&_td]:border [&_td]:border-gray-300 [&_td]:px-2 [&_td]:py-1 [&_th]:border [&_th]:border-gray-300 [&_th]:bg-gray-50 [&_th]:px-2 [&_th]:py-1 [&_th]:text-left [&_.selectedCell]:bg-brand/10 [&_.column-resize-handle]:bg-brand/40"
            />
          ) : mode === "markdown" ? (
            <textarea
              value={mdText}
              onChange={(e) => setMdText(e.target.value)}
              className="h-[24rem] w-full resize-none rounded-lg border border-gray-200 p-3 font-mono text-[12px] focus:border-brand focus:outline-none"
              spellCheck={false}
            />
          ) : (
            <div className="prose prose-sm max-w-none [&_img]:max-w-full [&_img]:rounded-lg">
              {previewMd.trim() ? (
                <Markdown components={previewComponents}>{previewMd}</Markdown>
              ) : (
                <p className="text-sm text-gray-400">Nothing to preview yet.</p>
              )}
            </div>
          )}
        </div>

        {err && <div className="border-t border-red-200 bg-red-50 px-4 py-2 text-[12px] text-red-700">{err}</div>}
        <div className="flex items-center gap-2 border-t px-4 py-3">
          <span className="text-[11px] text-gray-400">Mermaid &amp; images render in the document and the exported PDF.</span>
          <div className="ml-auto flex gap-2">
            <button onClick={() => !saving && onClose()} className="rounded-lg border px-3 py-1.5 text-sm text-gray-600 hover:bg-gray-50">Cancel</button>
            <button onClick={() => void save()} disabled={saving} className="rounded-lg bg-brand px-3 py-1.5 text-sm font-semibold text-white hover:bg-brand-dark disabled:opacity-50">{saving ? "Saving…" : "Save section"}</button>
          </div>
        </div>
      </div>
    </div>
  );
}
