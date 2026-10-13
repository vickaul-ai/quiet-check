export default function Home() {
  return (
    <main className="mx-auto flex max-w-2xl flex-1 flex-col justify-center gap-4 p-8">
      <h1 className="text-3xl font-semibold">Quiet Check</h1>
      <p>
        A merchant agent privately checks an AI-agent purchase against other merchants&apos; fraud
        lists, then uses PayPal to capture, hold or decline the payment.
      </p>
      <p className="text-sm text-neutral-500">
        Storefront arrives in D5 and the merchant console in D6.
      </p>
    </main>
  );
}
