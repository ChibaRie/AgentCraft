import { useEffect, useRef, useState } from "react";
import { CaretRight, Folder, HardDrives, X } from "@phosphor-icons/react";
import { request } from "../api/client.js";

/**
 * 工作目录选择器（Engineering Spec §6.6/§8.3）：
 * 只通过 GET /api/workspaces 浏览授权根目录内的子目录并提交相对路径，
 * 不提供系统目录上传或任意绝对路径输入。
 */
export default function WorkdirSelector({ value, onChange }) {
  const [open, setOpen] = useState(false);
  const [path, setPath] = useState("");
  const [entries, setEntries] = useState([]);
  const [error, setError] = useState(null);
  const [isLoading, setIsLoading] = useState(false);
  const panelRef = useRef(null);

  useEffect(() => {
    if (!open) {
      return undefined;
    }
    let cancelled = false;
    setIsLoading(true);
    setError(null);
    request(`/api/workspaces?path=${encodeURIComponent(path)}`)
      .then((payload) => {
        if (!cancelled) {
          setEntries(payload.data.directories);
        }
      })
      .catch((cause) => {
        if (!cancelled) {
          setEntries([]);
          setError(cause.message);
        }
      })
      .finally(() => {
        if (!cancelled) {
          setIsLoading(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [open, path]);

  useEffect(() => {
    if (!open) {
      return undefined;
    }
    function handlePointerDown(event) {
      if (panelRef.current && !panelRef.current.contains(event.target)) {
        setOpen(false);
      }
    }
    function handleKeyDown(event) {
      if (event.key === "Escape") {
        setOpen(false);
      }
    }
    document.addEventListener("pointerdown", handlePointerDown);
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("pointerdown", handlePointerDown);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [open]);

  const segments = path ? path.split("/") : [];
  const display = value ? value : "授权根目录（默认）";

  return (
    <div className="workdir" ref={panelRef}>
      <span className="field-label">工作目录</span>
      <button
        type="button"
        className="field-input workdir-trigger"
        aria-haspopup="dialog"
        aria-expanded={open}
        onClick={() => setOpen((current) => !current)}
      >
        <Folder size={15} aria-hidden="true" />
        <span className="workdir-trigger-text">{display}</span>
      </button>

      <div className="workdir-panel" role="dialog" aria-label="选择工作目录" hidden={!open}>
        <div className="workdir-bar">
          <button
            type="button"
            className="workdir-crumb"
            onClick={() => setPath("")}
            disabled={path === ""}
          >
            <HardDrives size={14} aria-hidden="true" />
            根目录
          </button>
          {segments.map((segment, index) => (
            <button
              type="button"
              key={segment + index}
              className="workdir-crumb"
              disabled={index === segments.length - 1}
              onClick={() => setPath(segments.slice(0, index + 1).join("/"))}
            >
              {segment}
            </button>
          ))}
          <button
            type="button"
            className="workdir-close"
            aria-label="关闭目录选择"
            onClick={() => setOpen(false)}
          >
            <X size={14} aria-hidden="true" />
          </button>
        </div>

        {isLoading && <p className="workdir-note">读取目录中…</p>}
        {error && <p className="workdir-note is-error">{error}</p>}
        {!isLoading && !error && (
          <ul className="workdir-list">
            {entries.length === 0 && <li className="workdir-note">没有子目录</li>}
            {entries.map((entry) => (
              <li key={entry.relative_path}>
                <button
                  type="button"
                  className="workdir-item"
                  onClick={() => setPath(entry.relative_path)}
                >
                  <Folder size={15} aria-hidden="true" />
                  <span>{entry.name}</span>
                  <CaretRight size={13} className="workdir-caret" aria-hidden="true" />
                </button>
              </li>
            ))}
          </ul>
        )}

        <button
          type="button"
          className="btn btn-primary btn-sm workdir-select"
          onClick={() => {
            onChange(path);
            setOpen(false);
          }}
        >
          使用当前目录
        </button>
      </div>
    </div>
  );
}
