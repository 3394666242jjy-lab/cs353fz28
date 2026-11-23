import { sampleAnalysis } from '../api/fixtures';
import StatusBadge from '../components/StatusBadge';
import AnalysisResults from '../components/AnalysisResults';

export default function AnalysisPanel() {
  return (
    <>
      <div className="section-heading">
        <h2>岗位要求与证据</h2>
        <StatusBadge label="合成契约样例" tone="warning" />
      </div>
      <p className="muted">以下为合成样例，并非根据你的资料生成的分析。</p>
      <AnalysisResults analysis={sampleAnalysis} sample />
    </>
  );
}
