import { Suspense } from "react";
import { JobView } from "@/components/JobView";

async function JobFromParams({ params }: { params: PageProps<"/jobs/[id]">["params"] }) {
  const { id } = await params;
  return <JobView jobId={id} />;
}

export default function JobPage({ params }: PageProps<"/jobs/[id]">) {
  return (
    <Suspense fallback={<p className="p-6 text-sm text-quiet">Loading the recruiter…</p>}>
      <JobFromParams params={params} />
    </Suspense>
  );
}
