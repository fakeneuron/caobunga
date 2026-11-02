import { afterEach, describe, expect, it, vi } from "vitest";

import { api, fetchDigest } from "../../src/api/client";

afterEach(() => {
  vi.restoreAllMocks();
});

describe("fetchDigest", () => {
  it("returns the digest body from the digest endpoint", async () => {
    const get = vi
      .spyOn(api, "get")
      .mockResolvedValue({ data: { markdown: "# Fleet digest" } });

    await expect(fetchDigest()).resolves.toEqual({ markdown: "# Fleet digest" });
    expect(get).toHaveBeenCalledWith("/api/digest");
  });
});
