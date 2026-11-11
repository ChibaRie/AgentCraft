import { useId, useState } from "react";
import { CaretDown } from "@phosphor-icons/react";

/**
 * 无 JS 高度计算的折叠原语：外层始终是 grid，0fr → 1fr 由 CSS 过渡。
 * 内容保持挂载（读屏可按展开状态感知），不测量 scrollHeight。
 */
export default function Collapsible({ title, defaultOpen = false, children }) {
  const [open, setOpen] = useState(defaultOpen);
  const contentId = useId();

  return (
    <div className="panel-collapse">
      <button
        type="button"
        className="collapse-trigger"
        aria-expanded={open}
        aria-controls={contentId}
        onClick={() => setOpen((value) => !value)}
      >
        <span>{title}</span>
        <CaretDown size={12} weight="bold" aria-hidden="true" />
      </button>
      <div className={"collapse" + (open ? " is-open" : "")} id={contentId}>
        <div>
          <div className="collapse-inner">{children}</div>
        </div>
      </div>
    </div>
  );
}
