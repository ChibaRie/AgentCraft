/** 专家分类（PRD §4.2.2 枚举）与展示文案。 */
export const CATEGORY_LABELS = {
  tech: "技术",
  design: "设计",
  writing: "写作",
  data_analysis: "数据分析",
  office: "办公效率",
  other: "其他",
};

export const CATEGORY_OPTIONS = Object.entries(CATEGORY_LABELS).map(([value, label]) => ({
  value,
  label,
}));

export const STATUS_LABELS = { draft: "草稿", published: "已发布", offline: "已下架" };
