/* Suggestions edit a draft. Only the parent form can save an event. */
(() => {
  "use strict";
  class DescriptionSuggestions {
    constructor({onChange,request = (url,options)=>globalThis.fetch(url,options)} = {}) {
      this.onChange = onChange;
      this.request = request;
      this.revision = 0;
      this.state = {phase:"editing",options:[],message:""};
    }

    reset(message = "") {
      this.revision++;
      this.controller?.abort();
      this.state = {phase:"editing",options:[],message};
      this.onChange?.(this.state);
    }

    async generate(draft,provider) {
      this.reset();
      if (!draft.name.trim() && !draft.description.trim()) {
        this.reset("Add an event name or a rough description first.");
        return;
      }
      const revision = this.revision;
      this.controller = new AbortController();
      this.state = {phase:"loading",options:[],message:"Writing three alternatives…"};
      this.onChange?.(this.state);
      try {
        const response = await this.request("/api/monitor/suggestions",{method:"POST",
          headers:{"Content-Type":"application/json"},body:JSON.stringify({...draft,provider}),
          signal:this.controller.signal});
        const result = await response.json();
        if (revision !== this.revision) return;
        if (!response.ok) throw new Error(result.error || "Suggestions are unavailable. You can keep editing.");
        if (!Array.isArray(result.suggestions) || result.suggestions.length!==3 || result.suggestions.some(option=>
          !option || typeof option.label!=="string" || !option.label.trim() || option.label.length>80 ||
          typeof option.description!=="string" || !option.description.trim() || option.description.length>1000)) {
          throw new Error("Three usable suggestions were not returned. You can keep editing.");
        }
        this.state = {phase:"choosing",options:result.suggestions,
          message:result.provider === "mock" ? "Mock templates · No AI request. Choose one to edit, or keep your wording."
            : `Muse Spark 1.3 Contributor · ${result.reasoning_effort || "minimal"} reasoning · Choose a rewrite to edit, or keep your wording.`};
        this.onChange?.(this.state);
      } catch (failure) {
        if (revision === this.revision) this.reset(failure.message);
      }
    }

    choose(index) {
      if (this.state.phase !== "choosing" || !this.state.options[index]) return null;
      const description = this.state.options[index].description;
      this.reset("Suggestion filled in. Edit it as needed, then save the event when you’re ready.");
      return description;
    }
  }

  if (typeof module !== "undefined" && module.exports) module.exports = DescriptionSuggestions;
  else window.DescriptionSuggestions = DescriptionSuggestions;
})();
